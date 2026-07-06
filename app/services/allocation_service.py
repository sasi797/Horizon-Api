from datetime import date
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import (
    AllocationState, AllocationLog, AttendanceRecord, Agent, Booking, AuditLog
)


async def get_allocation_state(db: AsyncSession) -> dict:
    today = date.today()

    # Current pointer
    state_result = await db.execute(select(AllocationState).where(AllocationState.id == 1))
    state = state_result.scalar_one()

    # Active pool
    pool_result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.shift))
        .join(AttendanceRecord, (AttendanceRecord.agent_id == Agent.id) & (AttendanceRecord.is_current == True))
        .where(
            AttendanceRecord.status == "present",
            AttendanceRecord.shift_date == today,
            Agent.is_active == True,
        )
        .order_by(Agent.roster_position)
    )
    pool: List[Agent] = pool_result.scalars().all()

    pool_size = len(pool)
    next_agent = pool[state.pointer % pool_size] if pool else None

    return {
        "pointer": state.pointer,
        "pool_size": pool_size,
        "next_agent": next_agent,
        "active_pool": pool,
    }


async def get_allocation_log(
    db: AsyncSession,
    agent_id: Optional[UUID] = None,
    shift_id: Optional[UUID] = None,
    log_date: Optional[date] = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[List[AllocationLog], int]:
    q = (
        select(AllocationLog)
        .options(
            selectinload(AllocationLog.agent),
            selectinload(AllocationLog.booking),
        )
        .order_by(AllocationLog.allocated_at.desc())
    )
    if agent_id:
        q = q.where(AllocationLog.agent_id == agent_id)
    if shift_id:
        q = q.where(AllocationLog.shift_id == shift_id)
    if log_date:
        q = q.where(func.date(AllocationLog.allocated_at) == log_date)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all(), total


async def get_pending_bookings(db: AsyncSession) -> List[Booking]:
    result = await db.execute(
        select(Booking)
        .options(selectinload(Booking.attachments))
        .where(Booking.status == "received", Booking.assigned_agent_id == None)
        .order_by(Booking.received_at)
    )
    return result.scalars().all()


async def manual_assign(
    db: AsyncSession,
    booking: Booking,
    agent: Agent,
    supervisor: Agent,
) -> AllocationLog:
    from datetime import datetime, timezone
    from app.models.models import AllocationState

    state_result = await db.execute(select(AllocationState).where(AllocationState.id == 1))
    state = state_result.scalar_one()

    booking.assigned_agent_id = agent.id
    booking.status = "allocated"
    booking.allocated_at = datetime.now(timezone.utc)
    await db.flush()

    log = AllocationLog(
        booking_id=booking.id,
        agent_id=agent.id,
        shift_id=agent.shift_id,
        pointer_value=state.pointer,
        pool_size=0,
        method="manual_supervisor",
        manual_by=supervisor.id,
    )
    db.add(log)
    await db.flush()

    db.add(AuditLog(
        entity_type="booking",
        entity_id=booking.id,
        event="booking.manually_assigned",
        actor_id=supervisor.id,
        payload={"agent_id": str(agent.id), "agent_code": agent.agent_code},
    ))
    await db.flush()
    return log


async def get_fairness_report(
    db: AsyncSession,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[dict]:
    q = select(
        AllocationLog.agent_id,
        func.count(AllocationLog.id).label("total"),
    ).group_by(AllocationLog.agent_id)

    if start_date:
        q = q.where(func.date(AllocationLog.allocated_at) >= start_date)
    if end_date:
        q = q.where(func.date(AllocationLog.allocated_at) <= end_date)

    result = await db.execute(q)
    rows = result.all()

    agents_result = await db.execute(select(Agent).options(selectinload(Agent.shift)))
    agents = {a.id: a for a in agents_result.scalars()}

    return [
        {"agent": agents.get(r.agent_id), "total_bookings": r.total}
        for r in rows
        if r.agent_id in agents
    ]
