from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import get_current_agent
from app.models.models import Booking, AllocationLog, AttendanceRecord, Agent

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/bookings")
async def bookings_report(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    q = select(
        Booking.priority,
        Booking.status,
        func.count(Booking.id).label("count")
    ).group_by(Booking.priority, Booking.status)

    if start_date:
        q = q.where(func.date(Booking.received_at) >= start_date)
    if end_date:
        q = q.where(func.date(Booking.received_at) <= end_date)

    result = await db.execute(q)
    rows = result.all()
    return [{"priority": r.priority, "status": r.status, "count": r.count} for r in rows]


@router.get("/attendance")
async def attendance_report(
    start_date: date = Query(...),
    end_date: date = Query(...),
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    result = await db.execute(
        select(
            AttendanceRecord.shift_date,
            AttendanceRecord.shift_id,
            AttendanceRecord.status,
            func.count(AttendanceRecord.id).label("count")
        )
        .where(
            AttendanceRecord.shift_date >= start_date,
            AttendanceRecord.shift_date <= end_date,
            AttendanceRecord.is_current == True,
        )
        .group_by(AttendanceRecord.shift_date, AttendanceRecord.shift_id, AttendanceRecord.status)
        .order_by(AttendanceRecord.shift_date)
    )
    rows = result.all()
    return [
        {
            "date": str(r.shift_date),
            "shift_id": str(r.shift_id),
            "status": r.status,
            "count": r.count,
        }
        for r in rows
    ]


@router.get("/fairness")
async def fairness_report(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    from app.services.allocation_service import get_fairness_report
    rows = await get_fairness_report(db, start_date, end_date)
    return [
        {
            "agent_code": r["agent"].agent_code,
            "full_name": r["agent"].full_name,
            "shift": r["agent"].shift_id,
            "total_bookings": r["total_bookings"],
        }
        for r in rows if r["agent"]
    ]


@router.get("/sla")
async def sla_report(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    q = select(
        Booking.priority,
        func.count(Booking.id).label("total"),
        func.sum(
            func.cast(
                (Booking.sla_deadline_at > func.coalesce(Booking.submitted_at, now)),
                func.Integer()
            )
        ).label("within_sla")
    ).where(
        Booking.status.in_(["submitted", "confirmed"])
    ).group_by(Booking.priority)

    if start_date:
        q = q.where(func.date(Booking.received_at) >= start_date)
    if end_date:
        q = q.where(func.date(Booking.received_at) <= end_date)

    result = await db.execute(q)
    rows = result.all()
    return [
        {
            "priority": r.priority,
            "total": r.total,
            "within_sla": r.within_sla or 0,
            "breached": r.total - (r.within_sla or 0),
            "compliance_pct": round((r.within_sla or 0) / r.total * 100, 1) if r.total else 0,
        }
        for r in rows
    ]
