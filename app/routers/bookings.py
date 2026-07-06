import json
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_current_user, get_db
from app.models.agent import Agent
from app.models.allocation import AllocationLog
from app.models.booking import Booking, BookingEvent, booking_support_agents_table
from app.models.booking_read import BookingRead
from app.models.email_message import EmailAttachment, EmailMessage
from app.models.user import User
from app.redis_client import get_redis
from app.schemas.booking import BookingCreate, BookingEventOut, BookingListOut, BookingOut, BookingPageOut, BookingStatusUpdate, BookingUpdate
from app.utils.notify import notify_roles, notify_user

STATS_CACHE_KEY = "bts:dashboard:stats"

router = APIRouter(prefix="/bookings", tags=["bookings"])


def _generate_id() -> str:
    import random
    num = random.randint(0, 9999999)
    return f"LW{num:07d}"


async def _send_notifications(db: AsyncSession, coros, redis=None) -> None:
    """Run notification inserts after the main commit. Never raises."""
    try:
        for coro in coros:
            await coro
        await db.commit()
        if redis:
            await redis.publish("bts:events", json.dumps({"type": "notification"}))
    except Exception:
        await db.rollback()


async def _mark_read(db: AsyncSession, user_id, booking_id: str, read_at: datetime) -> None:
    """Mark a booking as read for the acting user so their own change doesn't show as unread."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    try:
        await db.execute(
            pg_insert(BookingRead)
            .values(user_id=user_id, booking_id=booking_id, read_at=read_at)
            .on_conflict_do_update(index_elements=["user_id", "booking_id"], set_={"read_at": read_at})
        )
        await db.commit()
    except Exception:
        await db.rollback()


@router.get("", response_model=BookingPageOut)
async def list_bookings(
    status: str | None = Query(None),
    priority: str | None = Query(None),
    sender_email: str | None = Query(None),
    agent_id: str | None = Query(None),
    search: str | None = Query(None),
    created_after: str | None = Query(None),  # today | yesterday | 2d | 7d | 30d | date:YYYY-MM-DD
    closed_after: str | None = Query(None),   # today | week | month | date:YYYY-MM-DD
    tz: str = Query("UTC"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import zoneinfo
    from uuid import UUID
    from sqlalchemy import or_

    try:
        local_tz = zoneinfo.ZoneInfo(tz)
    except Exception:
        local_tz = zoneinfo.ZoneInfo("UTC")

    q = select(Booking).options(selectinload(Booking.agent)).order_by(Booking.last_email_at.desc())

    if agent_id:
        q = q.where(Booking.agent_id == UUID(agent_id))

    if status:
        q = q.where(Booking.status == status)
    if priority:
        q = q.where(Booking.priority == priority)
    if sender_email:
        q = q.where(Booking.sender_email == sender_email)
    if search:
        s = f'%{search}%'
        q = q.where(or_(
            Booking.id.ilike(s),
            Booking.subject.ilike(s),
            Booking.sender_email.ilike(s),
            Booking.da_number.ilike(s),
            Booking.tags.ilike(s),
        ))

    now = datetime.now(local_tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if created_after == 'today':
        q = q.where(Booking.received_at >= today_start)
    elif created_after == 'yesterday':
        yesterday_start = today_start - timedelta(days=1)
        q = q.where(Booking.received_at >= yesterday_start, Booking.received_at < today_start)
    elif created_after == '2d':
        two_days_start = today_start - timedelta(days=2)
        yesterday_start = today_start - timedelta(days=1)
        q = q.where(Booking.received_at >= two_days_start, Booking.received_at < yesterday_start)
    elif created_after == '7d':
        q = q.where(Booking.received_at >= now - timedelta(days=7))
    elif created_after == '30d':
        q = q.where(Booking.received_at >= now - timedelta(days=30))
    elif created_after and created_after.startswith('date:'):
        try:
            day_start = datetime.strptime(created_after[5:], '%Y-%m-%d').replace(tzinfo=local_tz)
            day_end = day_start + timedelta(days=1)
            q = q.where(Booking.received_at >= day_start, Booking.received_at < day_end)
        except ValueError:
            pass

    if closed_after == 'today':
        q = q.where(Booking.completed_at >= today_start)
    elif closed_after == 'week':
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.where(Booking.completed_at >= week_start)
    elif closed_after == 'month':
        q = q.where(Booking.completed_at >= now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    elif closed_after and closed_after.startswith('date:'):
        try:
            day_start = datetime.strptime(closed_after[5:], '%Y-%m-%d').replace(tzinfo=local_tz)
            day_end = day_start + timedelta(days=1)
            q = q.where(Booking.completed_at >= day_start, Booking.completed_at < day_end)
        except ValueError:
            pass

    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()

    items_q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(items_q)
    items = result.scalars().all()

    booking_ids = [b.id for b in items]
    reads_result = await db.execute(
        select(BookingRead).where(
            BookingRead.user_id == current_user.id,
            BookingRead.booking_id.in_(booking_ids),
        )
    )
    read_map = {r.booking_id: r.read_at for r in reads_result.scalars().all()}

    email_counts_result = await db.execute(
        select(EmailMessage.booking_id, func.count(EmailMessage.id).label("cnt"))
        .where(EmailMessage.booking_id.in_(booking_ids))
        .group_by(EmailMessage.booking_id)
    )
    email_count_map = {row.booking_id: row.cnt for row in email_counts_result}

    def _is_read(b: Booking) -> bool:
        r = read_map.get(b.id)
        return r is not None and r >= b.last_email_at

    def _has_reply(b: Booking) -> bool:
        return (email_count_map.get(b.id) or 0) > 1

    children_result = await db.execute(
        select(Booking.parent_booking_id)
        .where(Booking.parent_booking_id.in_(booking_ids))
        .distinct()
    )
    has_children_ids = {row[0] for row in children_result}

    return BookingPageOut(
        items=[BookingListOut.model_validate(b).model_copy(update={
            "is_read": _is_read(b),
            "has_reply": _has_reply(b),
            "has_children": b.id in has_children_ids,
        }) for b in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total > 0 else 1,
    )


@router.post("/mark-all-read", status_code=204)
async def mark_all_read(
    booking_ids: list[str],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    now = datetime.now(timezone.utc)
    for bid in booking_ids:
        await db.execute(
            pg_insert(BookingRead)
            .values(user_id=current_user.id, booking_id=bid, read_at=now)
            .on_conflict_do_update(index_elements=["user_id", "booking_id"], set_={"read_at": now})
        )
    await db.commit()


@router.post("/{booking_id}/mark-read", status_code=204)
async def mark_booking_read(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    now = datetime.now(timezone.utc)
    await db.execute(
        pg_insert(BookingRead)
        .values(user_id=current_user.id, booking_id=booking_id, read_at=now)
        .on_conflict_do_update(index_elements=["user_id", "booking_id"], set_={"read_at": now})
    )
    await db.commit()


@router.post("", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
async def create_booking(
    body: BookingCreate,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    booking_id = body.id or _generate_id()
    existing = await db.get(Booking, booking_id)
    if existing:
        raise HTTPException(status_code=400, detail="Booking ID already exists")

    booking = Booking(
        id=booking_id,
        subject=body.subject,
        priority=body.priority,
        sender_email=body.sender_email,
        parent_booking_id=body.parent_booking_id,
        source_message_id=body.source_message_id,
    )
    db.add(booking)
    db.add(BookingEvent(booking_id=booking_id, event="created", actor_name=current_user.name, new_value="Pending"))
    if body.parent_booking_id:
        db.add(BookingEvent(
            booking_id=booking_id,
            event="child_booking_created",
            actor_name=current_user.name,
            new_value=body.parent_booking_id,
        ))
        db.add(BookingEvent(
            booking_id=body.parent_booking_id,
            event="child_booking_created",
            actor_name=current_user.name,
            new_value=booking_id,
        ))
    # Copy source email message + attachments into the child booking so the
    # triggering email is visible in its thread
    if body.source_message_id:
        src_result = await db.execute(
            select(EmailMessage)
            .options(selectinload(EmailMessage.attachments))
            .where(EmailMessage.id == body.source_message_id)
        )
        src = src_result.scalar_one_or_none()
        if src:
            import uuid as _uuid
            new_msg = EmailMessage(
                id=_uuid.uuid4(),
                booking_id=booking_id,
                message_id=src.message_id,
                in_reply_to=src.in_reply_to,
                conversation_id=src.conversation_id,
                graph_message_id=src.graph_message_id,
                direction=src.direction,
                from_email=src.from_email,
                to_email=src.to_email,
                cc_emails=src.cc_emails,
                subject=src.subject,
                body_text=src.body_text,
                body_html=src.body_html,
                sent_at=src.sent_at,
            )
            db.add(new_msg)
            for att in src.attachments:
                db.add(EmailAttachment(
                    id=_uuid.uuid4(),
                    message_id=new_msg.id,
                    filename=att.filename,
                    content_type=att.content_type,
                    size_bytes=att.size_bytes,
                    storage_path=att.storage_path,
                ))

    await db.commit()
    await db.refresh(booking)
    await redis.delete(STATS_CACHE_KEY)
    await redis.publish("bts:events", json.dumps({"type": "booking_event", "booking_id": booking_id}))
    if body.parent_booking_id:
        await redis.publish("bts:events", json.dumps({"type": "booking_event", "booking_id": body.parent_booking_id}))

    await _send_notifications(db, [
        notify_roles(db, ['admin', 'supervisor'],
            "New booking received",
            f"Booking {booking_id} — {body.subject}",
            "booking_created", booking_id),
    ], redis=redis)
    return booking


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Booking)
        .options(
            selectinload(Booking.agent),
            selectinload(Booking.support_agents),
            selectinload(Booking.parent_booking),
            selectinload(Booking.child_bookings),
        )
        .where(Booking.id == booking_id)
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


@router.put("/{booking_id}", response_model=BookingOut)
async def update_booking(
    booking_id: str,
    body: BookingUpdate,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Booking).options(selectinload(Booking.agent)).where(Booking.id == booking_id)
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    prev_agent_id = booking.agent_id
    prev_status = booking.status
    prev_priority = booking.priority
    subject = booking.subject  # capture before model_dump overwrites it
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(booking, field, value)

    if body.status in ("Completed", "Ignored") and booking.completed_at is None:
        booking.completed_at = datetime.now(timezone.utc)
    if body.agent_id and booking.assigned_at is None:
        booking.assigned_at = datetime.now(timezone.utc)

    # Write history events
    if body.status is not None and body.status != prev_status:
        db.add(BookingEvent(booking_id=booking_id, event="status_changed", actor_name=current_user.name, old_value=prev_status, new_value=body.status))
    if body.priority is not None and body.priority != prev_priority:
        db.add(BookingEvent(booking_id=booking_id, event="priority_changed", actor_name=current_user.name, old_value=prev_priority, new_value=body.priority))

    # Collect allocation log + notification context before commit
    notify_coros = []
    if body.agent_id is None and prev_agent_id is not None and 'agent_id' in body.model_fields_set:
        db.add(BookingEvent(booking_id=booking_id, event="agent_unassigned", actor_name=current_user.name))
        if 'status' not in body.model_fields_set and prev_status == "In Progress":
            booking.status = "Open"
            db.add(BookingEvent(booking_id=booking_id, event="status_changed", actor_name=current_user.name, old_value=prev_status, new_value="Open"))
    if body.agent_id is not None:
        from uuid import UUID as _UUID
        new_agent_id = _UUID(str(body.agent_id))
        print(f"[DEBUG assign] booking={booking_id} prev_status={prev_status!r} model_fields_set={body.model_fields_set}")
        if 'status' not in body.model_fields_set and prev_status == "Open":
            booking.status = "In Progress"
            print(f"[DEBUG assign] AUTO status → In Progress")
            db.add(BookingEvent(booking_id=booking_id, event="status_changed", actor_name=current_user.name, old_value=prev_status, new_value="In Progress"))
        if new_agent_id != prev_agent_id:
            db.add(AllocationLog(
                booking_id=booking_id,
                agent_id=new_agent_id,
                pointer_value=-1,
                pool_size=0,
            ))
            agent_res = await db.execute(select(Agent).where(Agent.id == new_agent_id))
            assigned_agent = agent_res.scalar_one_or_none()
            db.add(BookingEvent(booking_id=booking_id, event="agent_assigned", actor_name=current_user.name, new_value=assigned_agent.name if assigned_agent else None))
            if assigned_agent and assigned_agent.user_id:
                notify_coros.append(notify_user(db, assigned_agent.user_id,
                    "Booking assigned to you",
                    f"Booking {booking_id} — {subject} has been assigned to you",
                    "booking_assigned", booking_id))

    if body.status == "Completed":
        notify_coros.append(notify_roles(db, ['admin', 'supervisor'],
            "Booking completed",
            f"Booking {booking_id} — {subject} has been marked as completed",
            "booking_completed", booking_id))

    if body.priority is not None and body.priority != prev_priority:
        notify_coros.append(notify_roles(db, ['admin', 'supervisor', 'agent'],
            "Booking priority changed",
            f"Booking {booking_id} — {subject}: priority {prev_priority} → {body.priority}",
            "priority_changed", booking_id))

    await db.commit()
    await db.refresh(booking)
    await redis.delete(STATS_CACHE_KEY)
    await _mark_read(db, current_user.id, booking_id, booking.updated_at)
    await redis.publish("bts:events", json.dumps({"type": "booking_event", "booking_id": booking_id}))

    if notify_coros:
        await _send_notifications(db, notify_coros, redis=redis)

    return booking


@router.patch("/{booking_id}/account-code", response_model=BookingOut)
async def set_account_code(
    booking_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Booking).options(selectinload(Booking.agent), selectinload(Booking.support_agents)).where(Booking.id == booking_id)
    )
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking.account_code = body.get("code")
    await db.commit()
    await db.refresh(booking)
    return booking


@router.patch("/{booking_id}/status", response_model=BookingOut)
async def update_status(
    booking_id: str,
    body: BookingStatusUpdate,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Booking).options(selectinload(Booking.agent)).where(Booking.id == booking_id)
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    subject = booking.subject
    prev_status = booking.status
    booking.status = body.status
    if body.status in ("Completed", "Ignored"):
        booking.completed_at = datetime.now(timezone.utc)
    if body.status == "Completed":
        if body.da_number:
            booking.da_number = body.da_number
        if body.da_description:
            booking.da_description = body.da_description

    db.add(BookingEvent(booking_id=booking_id, event="status_changed", actor_name=current_user.name, old_value=prev_status, new_value=body.status))
    await db.commit()
    await db.refresh(booking)
    await redis.delete(STATS_CACHE_KEY)
    await _mark_read(db, current_user.id, booking_id, booking.updated_at)
    await redis.publish("bts:events", json.dumps({"type": "booking_event", "booking_id": booking_id}))

    notify_coros = []
    if body.status == "Completed":
        notify_coros.append(notify_roles(db, ['admin', 'supervisor', 'agent'],
            "Booking completed",
            f"Booking {booking_id} — {subject} has been marked as completed",
            "booking_completed", booking_id))
    else:
        notify_coros.append(notify_roles(db, ['admin', 'supervisor', 'agent'],
            "Booking status updated",
            f"Booking {booking_id} — {subject}: {prev_status} → {body.status}",
            "status_changed", booking_id))
    await _send_notifications(db, notify_coros, redis=redis)

    return booking


@router.patch("/{booking_id}/assign", response_model=BookingOut)
async def assign_agent(
    booking_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    from uuid import UUID
    result = await db.execute(
        select(Booking).options(selectinload(Booking.agent)).where(Booking.id == booking_id)
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    subject = booking.subject
    prev_agent_id = booking.agent_id
    booking.agent_id = UUID(body["agent_id"]) if body.get("agent_id") else None

    notify_coros = []
    prev_status = booking.status
    if booking.agent_id:
        if booking.status == "Open":
            booking.status = "In Progress"
            db.add(BookingEvent(booking_id=booking_id, event="status_changed", actor_name=current_user.name, old_value=prev_status, new_value="In Progress"))
        booking.assigned_at = datetime.now(timezone.utc)
        if booking.agent_id != prev_agent_id:
            db.add(AllocationLog(
                booking_id=booking_id,
                agent_id=booking.agent_id,
                pointer_value=-1,
                pool_size=0,
            ))
            agent_res = await db.execute(select(Agent).where(Agent.id == booking.agent_id))
            assigned_agent = agent_res.scalar_one_or_none()
            db.add(BookingEvent(booking_id=booking_id, event="agent_assigned", actor_name=current_user.name, new_value=assigned_agent.name if assigned_agent else None))
            if assigned_agent and assigned_agent.user_id:
                notify_coros.append(notify_user(db, assigned_agent.user_id,
                    "Booking assigned to you",
                    f"Booking {booking_id} — {subject} has been assigned to you",
                    "booking_assigned", booking_id))
            notify_coros.append(notify_roles(db, ['admin', 'supervisor'],
                "Booking reassigned",
                f"Booking {booking_id} — {subject} assigned to {assigned_agent.name if assigned_agent else 'agent'} by {current_user.name}",
                "booking_assigned", booking_id))
    else:
        if booking.status == "In Progress":
            booking.status = "Open"
            db.add(BookingEvent(booking_id=booking_id, event="status_changed", actor_name=current_user.name, old_value=prev_status, new_value="Open"))
        db.add(BookingEvent(booking_id=booking_id, event="agent_unassigned", actor_name=current_user.name))
        notify_coros.append(notify_roles(db, ['admin', 'supervisor'],
            "Booking unassigned",
            f"Booking {booking_id} — {subject} unassigned by {current_user.name}",
            "booking_assigned", booking_id))

    await db.commit()
    await db.refresh(booking)
    await _mark_read(db, current_user.id, booking_id, booking.updated_at)
    await redis.publish("bts:events", json.dumps({"type": "booking_event", "booking_id": booking_id}))

    if notify_coros:
        await _send_notifications(db, notify_coros, redis=redis)

    return booking


@router.get("/{booking_id}/events", response_model=list[BookingEventOut])
async def get_booking_events(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(BookingEvent).where(BookingEvent.booking_id == booking_id).order_by(BookingEvent.created_at)
    )
    return result.scalars().all()


@router.post("/{booking_id}/support-agents", response_model=BookingOut)
async def add_support_agent(
    booking_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    from uuid import UUID
    result = await db.execute(
        select(Booking).options(selectinload(Booking.agent), selectinload(Booking.support_agents)).where(Booking.id == booking_id)
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    agent_id = UUID(body["agent_id"])
    if any(a.id == agent_id for a in booking.support_agents):
        raise HTTPException(status_code=400, detail="Agent is already a support agent")

    agent_res = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_res.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    booking.support_agents.append(agent)
    db.add(BookingEvent(booking_id=booking_id, event="support_agent_added", actor_name=current_user.name, new_value=agent.name))
    await db.commit()
    await db.refresh(booking)
    await _mark_read(db, current_user.id, booking_id, booking.updated_at)
    await redis.publish("bts:events", json.dumps({"type": "booking_event", "booking_id": booking_id}))

    notify_coros = []
    if agent.user_id and agent.user_id != current_user.id:
        notify_coros.append(notify_user(db, agent.user_id,
            "Added as support agent",
            f"You have been added as a support agent on Booking {booking_id} — {booking.subject}",
            "booking_assigned", booking_id))
    if booking.agent and booking.agent.user_id and booking.agent.user_id != current_user.id:
        notify_coros.append(notify_user(db, booking.agent.user_id,
            "Support agent added",
            f"{agent.name} was added as support on Booking {booking_id} — {booking.subject}",
            "booking_assigned", booking_id))
    notify_coros.append(notify_roles(db, ['admin', 'supervisor'],
        "Support agent added",
        f"{agent.name} added as support on Booking {booking_id} — {booking.subject} by {current_user.name}",
        "booking_assigned", booking_id))
    if notify_coros:
        await _send_notifications(db, notify_coros, redis=redis)

    return booking


@router.delete("/{booking_id}/support-agents/{agent_id}", response_model=BookingOut)
async def remove_support_agent(
    booking_id: str,
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    from uuid import UUID
    result = await db.execute(
        select(Booking).options(selectinload(Booking.agent), selectinload(Booking.support_agents)).where(Booking.id == booking_id)
    )
    booking = result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    agent_uuid = UUID(agent_id)
    agent = next((a for a in booking.support_agents if a.id == agent_uuid), None)
    if agent is None:
        raise HTTPException(status_code=404, detail="Support agent not found")

    booking.support_agents.remove(agent)
    db.add(BookingEvent(booking_id=booking_id, event="support_agent_removed", actor_name=current_user.name, old_value=agent.name))
    await db.commit()
    await db.refresh(booking)
    await _mark_read(db, current_user.id, booking_id, booking.updated_at)
    await redis.publish("bts:events", json.dumps({"type": "booking_event", "booking_id": booking_id}))

    notify_coros = []
    if agent.user_id and agent.user_id != current_user.id:
        notify_coros.append(notify_user(db, agent.user_id,
            "Removed as support agent",
            f"You have been removed from Booking {booking_id} — {booking.subject}",
            "booking_assigned", booking_id))
    notify_coros.append(notify_roles(db, ['admin', 'supervisor'],
        "Support agent removed",
        f"{agent.name} removed from Booking {booking_id} — {booking.subject} by {current_user.name}",
        "booking_assigned", booking_id))
    if notify_coros:
        await _send_notifications(db, notify_coros, redis=redis)

    return booking


@router.delete("/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_booking(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    booking = await db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    await db.delete(booking)
    await db.commit()
    await redis.delete(STATS_CACHE_KEY)
