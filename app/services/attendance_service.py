from datetime import date, datetime, timezone
from typing import Optional, List
from uuid import UUID

from sqlalchemy import select, update, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import AttendanceRecord, Agent, Shift, AuditLog


async def get_today_attendance(db: AsyncSession, shift_id: Optional[UUID] = None):
    today = date.today()
    q = (
        select(AttendanceRecord)
        .options(selectinload(AttendanceRecord.agent), selectinload(AttendanceRecord.shift))
        .where(AttendanceRecord.shift_date == today, AttendanceRecord.is_current == True)
    )
    if shift_id:
        q = q.where(AttendanceRecord.shift_id == shift_id)
    result = await db.execute(q)
    return result.scalars().all()


async def get_current_status(db: AsyncSession, agent_id: UUID, shift_date: date) -> Optional[AttendanceRecord]:
    result = await db.execute(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.agent_id == agent_id,
            AttendanceRecord.shift_date == shift_date,
            AttendanceRecord.is_current == True,
        )
    )
    return result.scalar_one_or_none()


async def mark_attendance(
    db: AsyncSession,
    agent_id: UUID,
    shift_id: UUID,
    status: str,
    marked_by_id: UUID,
    shift_date: Optional[date] = None,
    requires_auth: bool = False,
) -> AttendanceRecord:
    if shift_date is None:
        shift_date = date.today()

    # Expire any current record for this agent on this date
    await db.execute(
        update(AttendanceRecord)
        .where(
            AttendanceRecord.agent_id == agent_id,
            AttendanceRecord.shift_date == shift_date,
            AttendanceRecord.is_current == True,
        )
        .values(is_current=False)
    )
    await db.flush()

    # Get superseded record id
    prev_result = await db.execute(
        select(AttendanceRecord.id)
        .where(
            AttendanceRecord.agent_id == agent_id,
            AttendanceRecord.shift_date == shift_date,
        )
        .order_by(AttendanceRecord.marked_at.desc())
        .limit(1)
    )
    prev_id = prev_result.scalar_one_or_none()

    record = AttendanceRecord(
        agent_id=agent_id,
        shift_id=shift_id,
        shift_date=shift_date,
        status=status,
        marked_by=marked_by_id,
        is_current=True,
        supersedes_id=prev_id,
        requires_auth=requires_auth,
    )
    db.add(record)
    await db.flush()

    db.add(AuditLog(
        entity_type="attendance",
        entity_id=record.id,
        event=f"attendance.marked_{status}",
        actor_id=marked_by_id,
        payload={"agent_id": str(agent_id), "shift_date": str(shift_date), "status": status},
    ))
    await db.flush()
    return record


async def get_attendance_history(
    db: AsyncSession,
    agent_id: UUID,
    start_date: date,
    end_date: date,
) -> List[AttendanceRecord]:
    result = await db.execute(
        select(AttendanceRecord)
        .options(selectinload(AttendanceRecord.shift))
        .where(
            AttendanceRecord.agent_id == agent_id,
            AttendanceRecord.shift_date >= start_date,
            AttendanceRecord.shift_date <= end_date,
            AttendanceRecord.is_current == True,
        )
        .order_by(AttendanceRecord.shift_date.desc())
    )
    return result.scalars().all()


async def build_daily_summary(db: AsyncSession, target_date: date):
    from app.schemas.schemas import DailyAttendanceSummary, ShiftAttendanceSummary

    shifts_result = await db.execute(select(Shift))
    shifts = shifts_result.scalars().all()

    shift_summaries = []
    total_agents = 0
    total_present = 0
    total_absent = 0

    for shift in shifts:
        agents_result = await db.execute(
            select(Agent).where(Agent.shift_id == shift.id, Agent.is_active == True)
        )
        agents = agents_result.scalars().all()
        agent_ids = [a.id for a in agents]

        if not agent_ids:
            continue

        records_result = await db.execute(
            select(AttendanceRecord)
            .options(selectinload(AttendanceRecord.agent))
            .where(
                AttendanceRecord.shift_id == shift.id,
                AttendanceRecord.shift_date == target_date,
                AttendanceRecord.is_current == True,
                AttendanceRecord.agent_id.in_(agent_ids),
            )
        )
        records = records_result.scalars().all()

        present_count = sum(1 for r in records if r.status == "present")
        absent_count = sum(1 for r in records if r.status == "absent")
        on_break_count = sum(1 for r in records if r.status == "on_break")

        total_agents += len(agents)
        total_present += present_count
        total_absent += absent_count

        coverage_pct = (present_count / len(agents) * 100) if agents else 0

        shift_summaries.append({
            "shift": shift,
            "date": target_date,
            "agents": records,
            "present_count": present_count,
            "absent_count": absent_count,
            "on_break_count": on_break_count,
            "coverage_pct": round(coverage_pct, 1),
        })

    overall_coverage = (total_present / total_agents * 100) if total_agents else 0

    return {
        "date": target_date,
        "total_agents": total_agents,
        "total_present": total_present,
        "total_absent": total_absent,
        "coverage_pct": round(overall_coverage, 1),
        "shifts": shift_summaries,
    }
