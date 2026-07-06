import zoneinfo
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_current_user, get_db
from app.models.agent import Agent
from app.models.booking import Booking
from app.models.role import Role
from app.models.user import User
from app.schemas.agent import AgentCreate, AgentOut, AgentUpdate
from app.utils.jwt import hash_password


def _agent_day_window(date_str: str, tz_str: str) -> tuple[datetime, datetime]:
    try:
        local_tz = zoneinfo.ZoneInfo(tz_str)
    except Exception:
        local_tz = zoneinfo.ZoneInfo("UTC")
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=local_tz)
    return day_start, day_start + timedelta(days=1)

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentOut])
async def list_agents(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.shift), selectinload(Agent.user).selectinload(User.role_obj))
        .where(Agent.is_active == True)
        .order_by(Agent.name)
    )
    return result.scalars().all()


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    existing_agent = await db.execute(select(Agent).where(Agent.email == body.email))
    if existing_agent.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Agent email already exists")

    existing_user = await db.execute(select(User).where(User.email == body.email))
    if existing_user.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="A user account with this email already exists")

    role_result = await db.execute(select(Role).where(Role.key == body.role))
    role_obj = role_result.scalar_one_or_none()
    if role_obj is None:
        raise HTTPException(status_code=400, detail=f"Role '{body.role}' not found")

    user = User(
        name=body.name,
        email=body.email,
        password_hash=hash_password(body.password),
        role_id=role_obj.id,
    )
    db.add(user)
    await db.flush()

    agent = Agent(name=body.name, email=body.email, shift_id=body.shift_id, user_id=user.id)
    db.add(agent)
    await db.commit()
    result = await db.execute(
        select(Agent).options(selectinload(Agent.shift), selectinload(Agent.user).selectinload(User.role_obj)).where(Agent.id == agent.id)
    )
    return result.scalar_one()


@router.get("/stats")
async def agent_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
    date: str | None = Query(None, description="Single day (YYYY-MM-DD)"),
    date_from: str | None = Query(None, description="Range start (YYYY-MM-DD)"),
    date_to: str | None = Query(None, description="Range end (YYYY-MM-DD)"),
    tz: str = Query("UTC", description="IANA timezone"),
):
    agents_result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.shift))
        .where(Agent.is_active == True)
        .order_by(Agent.name)
    )
    agents = agents_result.scalars().all()
    if not agents:
        return []

    agent_ids = [a.id for a in agents]

    date_conds: list = []
    if date:
        day_start, day_end = _agent_day_window(date, tz)
        date_conds = [Booking.received_at >= day_start, Booking.received_at < day_end]
    elif date_from and date_to:
        day_start, _ = _agent_day_window(date_from, tz)
        _, day_end = _agent_day_window(date_to, tz)
        date_conds = [Booking.received_at >= day_start, Booking.received_at < day_end]

    stats_q = (
        select(
            Booking.agent_id,
            func.count(Booking.id).label("total"),
            func.count(case((Booking.status == "Pending", 1), else_=None)).label("pending"),
            func.count(case((Booking.status == "In Progress", 1), else_=None)).label("in_progress"),
            func.count(case((Booking.status == "Completed", 1), else_=None)).label("completed"),
            func.count(case((Booking.status == "Ignored", 1), else_=None)).label("ignored"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Booking.status == "Completed",
                                Booking.da_number.isnot(None),
                                Booking.da_number != "",
                            ),
                            func.array_length(func.string_to_array(Booking.da_number, ","), 1),
                        ),
                        else_=None,
                    )
                ),
                0,
            ).label("da_count"),
        )
        .where(Booking.agent_id.in_(agent_ids), *date_conds)
        .group_by(Booking.agent_id)
    )
    rows = (await db.execute(stats_q)).all()
    smap = {r.agent_id: r for r in rows}

    return [
        {
            "agent_id": str(a.id),
            "agent_name": a.name,
            "agent_email": a.email,
            "shift": a.shift.name if a.shift else None,
            "total": getattr(smap.get(a.id), "total", 0) or 0,
            "pending": getattr(smap.get(a.id), "pending", 0) or 0,
            "in_progress": getattr(smap.get(a.id), "in_progress", 0) or 0,
            "completed": getattr(smap.get(a.id), "completed", 0) or 0,
            "ignored": getattr(smap.get(a.id), "ignored", 0) or 0,
            "da_count": int(getattr(smap.get(a.id), "da_count", 0) or 0),
        }
        for a in agents
    ]


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    result = await db.execute(
        select(Agent).options(selectinload(Agent.shift), selectinload(Agent.user).selectinload(User.role_obj)).where(Agent.id == UUID(agent_id))
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.put("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    result = await db.execute(
        select(Agent).options(selectinload(Agent.shift), selectinload(Agent.user).selectinload(User.role_obj)).where(Agent.id == UUID(agent_id))
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    update_data = body.model_dump(exclude_unset=True)
    role = update_data.pop("role", None)
    for field, value in update_data.items():
        setattr(agent, field, value)
    if role and agent.user:
        role_result = await db.execute(select(Role).where(Role.key == role))
        role_obj = role_result.scalar_one_or_none()
        if role_obj is None:
            raise HTTPException(status_code=400, detail=f"Role '{role}' not found")
        agent.user.role_id = role_obj.id

    await db.commit()
    result = await db.execute(
        select(Agent).options(selectinload(Agent.shift), selectinload(Agent.user).selectinload(User.role_obj)).where(Agent.id == UUID(agent_id))
    )
    return result.scalar_one()


@router.get("/{agent_id}/bookings")
async def agent_bookings(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    from sqlalchemy import func
    from app.models.booking import Booking

    agent_uuid = UUID(agent_id)
    result = await db.execute(
        select(Booking).where(Booking.agent_id == agent_uuid).order_by(Booking.received_at.desc())
    )
    bookings = result.scalars().all()

    counts = {"Pending": 0, "In Progress": 0, "Completed": 0}
    for b in bookings:
        counts[b.status] = counts.get(b.status, 0) + 1

    return {
        "agent_id": agent_id,
        "total": len(bookings),
        "pending": counts["Pending"],
        "in_progress": counts["In Progress"],
        "completed": counts["Completed"],
        "bookings": [
            {
                "id": b.id,
                "subject": b.subject,
                "priority": b.priority,
                "status": b.status,
                "received_at": b.received_at,
                "assigned_at": b.assigned_at,
                "completed_at": b.completed_at,
            }
            for b in bookings
        ],
    }


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    agent = await db.get(Agent, UUID(agent_id))
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.is_active = False
    if agent.user_id:
        user = await db.get(User, agent.user_id)
        if user:
            user.is_active = False
    await db.commit()
