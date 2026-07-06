from datetime import datetime, timedelta, timezone, date
from typing import List, Optional
from uuid import UUID
import httpx

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import (
    Booking, BookingAttachment, BookingAnalysis,
    BookingIdSequence, AllocationState, AllocationLog,
    AttendanceRecord, Agent, AuditLog
)
from app.core.config import settings

SLA_WINDOWS = {
    "urgent":   timedelta(hours=1),
    "standard": timedelta(hours=4),
    "economy":  timedelta(days=1),
}


# ------------------------------------------------------------------ #
#  Booking ID generation (atomic)                                     #
# ------------------------------------------------------------------ #

async def generate_booking_id(db: AsyncSession) -> tuple[str, int, int]:
    year = datetime.now(timezone.utc).year

    # Upsert sequence row with lock
    result = await db.execute(
        select(BookingIdSequence).where(BookingIdSequence.year == year).with_for_update()
    )
    seq_row = result.scalar_one_or_none()

    if seq_row is None:
        seq_row = BookingIdSequence(year=year, last_sequence=0)
        db.add(seq_row)
        await db.flush()

    seq_row.last_sequence += 1
    await db.flush()

    booking_id = f"LW{seq_row.last_sequence:07d}"
    return booking_id, year, seq_row.last_sequence


# ------------------------------------------------------------------ #
#  Ingest (called by email poller task)                               #
# ------------------------------------------------------------------ #

async def ingest_booking(db: AsyncSession, data: dict) -> Booking:
    booking_id, year, seq = await generate_booking_id(db)
    priority = data.get("priority", "standard")
    sla_window = SLA_WINDOWS.get(priority, SLA_WINDOWS["standard"])

    booking = Booking(
        booking_id=booking_id,
        year=year,
        sequence_number=seq,
        sender_email=data["sender_email"],
        sender_name=data.get("sender_name"),
        email_subject=data.get("email_subject"),
        email_body=data.get("email_body"),
        priority=priority,
        status="received",
        sla_deadline_at=datetime.now(timezone.utc) + sla_window,
    )
    db.add(booking)
    await db.flush()

    for att in data.get("attachments", []):
        db.add(BookingAttachment(
            booking_id=booking.id,
            filename=att["filename"],
            storage_path=att["storage_path"],
            mime_type=att.get("mime_type"),
            size_bytes=att.get("size_bytes"),
        ))

    await db.flush()
    await _audit(db, "booking", booking.id, "booking.received", None, {"booking_id": booking_id})
    return booking


# ------------------------------------------------------------------ #
#  Round-robin allocation                                             #
# ------------------------------------------------------------------ #

async def allocate_booking(db: AsyncSession, booking: Booking) -> Optional[Agent]:
    """
    Returns assigned agent, or None if pool is empty (pending queue).
    Must be called inside a transaction.
    """
    today = date.today()

    # Get the active shift's present agents ordered by roster_position
    present_result = await db.execute(
        select(Agent)
        .join(AttendanceRecord, (AttendanceRecord.agent_id == Agent.id) & (AttendanceRecord.is_current == True))
        .where(
            AttendanceRecord.status == "present",
            AttendanceRecord.shift_date == today,
            Agent.is_active == True,
        )
        .order_by(Agent.roster_position)
    )
    pool: List[Agent] = present_result.scalars().all()

    if not pool:
        # No agents present — leave in received status, supervisor alerted via task
        await _audit(db, "booking", booking.id, "booking.pending_no_agents", None, {})
        return None

    # Get and lock pointer
    state_result = await db.execute(
        select(AllocationState).where(AllocationState.id == 1).with_for_update()
    )
    state = state_result.scalar_one()

    pool_size = len(pool)
    idx = state.pointer % pool_size
    assigned = pool[idx]

    # Advance pointer
    state.pointer += 1
    await db.flush()

    # Update booking
    booking.assigned_agent_id = assigned.id
    booking.status = "allocated"
    booking.allocated_at = datetime.now(timezone.utc)
    await db.flush()

    # Write allocation log
    db.add(AllocationLog(
        booking_id=booking.id,
        agent_id=assigned.id,
        shift_id=assigned.shift_id,
        pointer_value=state.pointer - 1,
        pool_size=pool_size,
        method="round_robin",
    ))
    await db.flush()
    await _audit(db, "booking", booking.id, "booking.allocated", assigned.id,
                 {"agent_code": assigned.agent_code, "pointer": state.pointer - 1, "pool_size": pool_size})

    return assigned


# ------------------------------------------------------------------ #
#  Get bookings                                                       #
# ------------------------------------------------------------------ #

async def get_bookings(
    db: AsyncSession,
    agent_id: Optional[UUID] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    sender_email: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[List[Booking], int]:
    q = select(Booking).options(
        selectinload(Booking.attachments),
        selectinload(Booking.assigned_agent),
    )
    if agent_id:
        q = q.where(Booking.assigned_agent_id == agent_id)
    if status:
        q = q.where(Booking.status == status)
    if priority:
        q = q.where(Booking.priority == priority)
    if sender_email:
        q = q.where(Booking.sender_email == sender_email)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.order_by(Booking.last_email_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all(), total


async def get_booking_by_id(db: AsyncSession, booking_id: str) -> Optional[Booking]:
    result = await db.execute(
        select(Booking)
        .options(
            selectinload(Booking.attachments),
            selectinload(Booking.assigned_agent).selectinload(Agent.shift),
            selectinload(Booking.analysis),
        )
        .where(Booking.booking_id == booking_id)
    )
    return result.scalar_one_or_none()


# ------------------------------------------------------------------ #
#  Analysis form                                                      #
# ------------------------------------------------------------------ #

async def upsert_analysis(
    db: AsyncSession,
    booking: Booking,
    agent: Agent,
    data: dict,
) -> BookingAnalysis:
    result = await db.execute(
        select(BookingAnalysis).where(BookingAnalysis.booking_id == booking.id)
    )
    analysis = result.scalar_one_or_none()

    if analysis is None:
        analysis = BookingAnalysis(booking_id=booking.id, agent_id=agent.id)
        db.add(analysis)

    for field in ["cargo_type", "weight_kg", "origin", "destination",
                  "required_dispatch_at", "vehicle_preference",
                  "handling_instructions", "agent_notes"]:
        if field in data and data[field] is not None:
            setattr(analysis, field, data[field])

    is_draft = data.get("is_draft", True)
    analysis.is_draft = is_draft

    if not is_draft:
        analysis.submitted_at = datetime.now(timezone.utc)
        booking.status = "in_review"

    await db.flush()
    event = "booking.analysis_draft" if is_draft else "booking.analysis_submitted"
    await _audit(db, "booking", booking.id, event, agent.id, {})
    return analysis


# ------------------------------------------------------------------ #
#  Transport submission                                               #
# ------------------------------------------------------------------ #

async def submit_to_transport(db: AsyncSession, booking: Booking, agent: Agent) -> str:
    """Submit to Transport API, store docket, return docket number."""
    if not booking.analysis:
        raise ValueError("Analysis must be completed before transport submission")

    payload = {
        "booking_id": booking.booking_id,
        "sender_email": booking.sender_email,
        "priority": booking.priority,
        "cargo_type": str(booking.analysis.cargo_type or ""),
        "weight_kg": float(booking.analysis.weight_kg or 0),
        "origin": booking.analysis.origin,
        "destination": booking.analysis.destination,
        "required_dispatch_at": booking.analysis.required_dispatch_at.isoformat() if booking.analysis.required_dispatch_at else None,
        "vehicle_preference": booking.analysis.vehicle_preference,
        "handling_instructions": booking.analysis.handling_instructions,
        "agent_notes": booking.analysis.agent_notes,
    }

    docket_number = await _call_transport_api(payload)

    booking.docket_number = docket_number
    booking.transport_payload = payload
    booking.status = "submitted"
    booking.submitted_at = datetime.now(timezone.utc)
    await db.flush()

    await _audit(db, "booking", booking.id, "booking.transport_submitted", agent.id,
                 {"docket_number": docket_number})
    return docket_number


async def _call_transport_api(payload: dict) -> str:
    """Call external Transport API with retries."""
    last_error = None
    for attempt in range(settings.TRANSPORT_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{settings.TRANSPORT_API_URL}/bookings",
                    json=payload,
                    headers={"X-API-Key": settings.TRANSPORT_API_KEY},
                )
                resp.raise_for_status()
                data = resp.json()
                return data["docket_number"]
        except Exception as e:
            last_error = e
            if attempt == settings.TRANSPORT_MAX_RETRIES - 1:
                break
    raise RuntimeError(f"Transport API failed after {settings.TRANSPORT_MAX_RETRIES} retries: {last_error}")


# ------------------------------------------------------------------ #
#  Audit helper                                                       #
# ------------------------------------------------------------------ #

async def _audit(db, entity_type, entity_id, event, actor_id, payload):
    db.add(AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        event=event,
        actor_id=actor_id,
        payload=payload,
    ))
    await db.flush()
