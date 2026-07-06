from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_current_user, get_db
from app.models.attendance import Attendance
from app.models.user import User
from app.schemas.attendance import AttendanceBulkUpdate, AttendanceOut, AttendanceSummary

router = APIRouter(prefix="/attendance", tags=["attendance"])


@router.get("", response_model=list[AttendanceOut])
async def get_attendance(
    date_val: date = Query(..., alias="date"),
    shift_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Attendance).options(selectinload(Attendance.agent)).where(Attendance.date == date_val)
    if shift_id:
        from uuid import UUID
        q = q.where(Attendance.shift_id == UUID(shift_id))
    result = await db.execute(q)
    return result.scalars().all()


@router.post("", response_model=list[AttendanceOut])
async def upsert_attendance(
    body: AttendanceBulkUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    for rec in body.records:
        resolved_shift_id = rec.shift_id or body.shift_id

        # Look up by (agent_id, date) only — shift_id is a field we're updating, not a key
        result = await db.execute(
            select(Attendance).where(
                Attendance.agent_id == rec.agent_id,
                Attendance.date == rec.date,
            )
        )
        rows = result.scalars().all()

        # Delete stale duplicates if any accumulated previously
        for dup in rows[1:]:
            await db.delete(dup)

        existing = rows[0] if rows else None

        if existing:
            existing.status = rec.status
            existing.check_in = rec.check_in
            existing.check_out = rec.check_out
            if resolved_shift_id is not None:
                existing.shift_id = resolved_shift_id
        else:
            db.add(Attendance(
                agent_id=rec.agent_id,
                shift_id=resolved_shift_id,
                date=rec.date,
                status=rec.status,
                check_in=rec.check_in,
                check_out=rec.check_out,
            ))

    await db.commit()

    q = (
        select(Attendance)
        .options(selectinload(Attendance.agent))
        .where(Attendance.date == body.date)
    )
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/summary", response_model=AttendanceSummary)
async def attendance_summary(
    date_val: date = Query(..., alias="date"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Attendance).where(Attendance.date == date_val)
    )
    records = result.scalars().all()
    counts = {"Present": 0, "Absent": 0, "On Break": 0, "Late": 0}
    for r in records:
        counts[r.status] = counts.get(r.status, 0) + 1
    return AttendanceSummary(
        date=date_val,
        present=counts["Present"],
        absent=counts["Absent"],
        on_break=counts["On Break"],
        late=counts["Late"],
        total=len(records),
    )
