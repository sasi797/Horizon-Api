from datetime import date, datetime, timedelta, timezone
from typing import Optional
import zoneinfo

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query
from sqlalchemy import Float, Integer, cast, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking
from app.models.user import User
from app.redis_client import get_redis
from app.schemas.reports import AvgCompletionReport, DailySummaryRow, HourlyPoint, PrioritySlice, ReportStats, StatusBreakdownRow, TrendPoint

router = APIRouter(prefix="/reports", tags=["reports"])


def _day_range(date_str: Optional[str], tz: str):
    """Return (day_start, day_end) for a single day in the given tz, or (None, None) for all time."""
    if not date_str:
        return None, None
    try:
        local_tz = zoneinfo.ZoneInfo(tz)
    except Exception:
        local_tz = zoneinfo.ZoneInfo("UTC")
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=local_tz)
    return day_start, day_start + timedelta(days=1)

PRIORITY_COLORS = {"Very Urgent": "#ef4444", "Urgent": "#f59e0b", "Not Urgent": "#22c55e"}


def _pct_change(cur: int, prev: int) -> float:
    if prev == 0:
        return 0.0 if cur == 0 else 100.0
    return round((cur - prev) / prev * 100, 1)


@router.get("/stats", response_model=ReportStats)
async def report_stats(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    cached = await redis.get("bts:reports:stats")
    if cached:
        import json
        return ReportStats(**json.loads(cached))

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    cur_start  = now - timedelta(days=7)
    prev_start = now - timedelta(days=14)
    sla_cutoff = now - timedelta(hours=24)
    prev_sla_cutoff = cur_start - timedelta(hours=24)

    # All-time totals (displayed values)
    total     = await db.scalar(select(func.count(Booking.id))) or 0
    completed = await db.scalar(select(func.count(Booking.id)).where(Booking.status == "Completed")) or 0
    pending   = await db.scalar(select(func.count(Booking.id)).where(Booking.status == "Pending")) or 0
    sla_breach = await db.scalar(
        select(func.count(Booking.id)).where(
            Booking.status.in_(["Pending", "In Progress"]),
            Booking.received_at < sla_cutoff,
        )
    ) or 0

    # Current 7-day window (for change calculation)
    cur_total     = await db.scalar(select(func.count(Booking.id)).where(Booking.received_at >= cur_start)) or 0
    cur_completed = await db.scalar(select(func.count(Booking.id)).where(Booking.completed_at >= cur_start)) or 0
    cur_pending   = await db.scalar(select(func.count(Booking.id)).where(Booking.received_at >= cur_start, Booking.status != "Completed")) or 0
    cur_sla       = await db.scalar(
        select(func.count(Booking.id)).where(
            Booking.received_at >= cur_start,
            Booking.status.in_(["Pending", "In Progress"]),
            Booking.received_at < sla_cutoff,
        )
    ) or 0

    # Previous 7-day window (for change calculation)
    prev_total     = await db.scalar(select(func.count(Booking.id)).where(Booking.received_at >= prev_start, Booking.received_at < cur_start)) or 0
    prev_completed = await db.scalar(select(func.count(Booking.id)).where(Booking.completed_at >= prev_start, Booking.completed_at < cur_start)) or 0
    prev_pending   = await db.scalar(select(func.count(Booking.id)).where(Booking.received_at >= prev_start, Booking.received_at < cur_start, Booking.status != "Completed")) or 0
    prev_sla       = await db.scalar(
        select(func.count(Booking.id)).where(
            Booking.received_at >= prev_start,
            Booking.received_at < cur_start,
            Booking.status.in_(["Pending", "In Progress"]),
            Booking.received_at < prev_sla_cutoff,
        )
    ) or 0

    rate = round((completed / total) * 100, 1) if total else 0.0
    stats = ReportStats(
        total_bookings=total,
        completed=completed,
        pending=pending,
        sla_breach=sla_breach,
        completion_rate=rate,
        total_bookings_change=_pct_change(cur_total, prev_total),
        completed_change=_pct_change(cur_completed, prev_completed),
        pending_change=_pct_change(cur_pending, prev_pending),
        sla_breach_change=_pct_change(cur_sla, prev_sla),
    )
    import json
    await redis.setex("bts:reports:stats", 300, json.dumps(stats.model_dump()))
    return stats


@router.get("/trend", response_model=list[TrendPoint])
async def bookings_trend(
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    end = date.today()
    start = end - timedelta(days=days - 1)

    result = await db.execute(
        select(
            func.date(Booking.received_at).label("day"),
            func.count(Booking.id).label("received"),
            func.sum(
                cast(case((Booking.status == "Completed", 1), else_=0), Integer)
            ).label("completed"),
        )
        .where(func.date(Booking.received_at) >= start)
        .group_by(func.date(Booking.received_at))
        .order_by(func.date(Booking.received_at))
    )
    rows = result.all()
    return [
        TrendPoint(
            date=row.day.strftime("%d %b").lstrip("0") if hasattr(row.day, "strftime") else str(row.day).split("T")[0],
            received=row.received,
            completed=row.completed or 0,
        )
        for row in rows
    ]


@router.get("/priority-distribution", response_model=list[PrioritySlice])
async def priority_distribution(
    date: Optional[str] = Query(None),
    tz: str = Query("UTC"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    day_start, day_end = _day_range(date, tz)
    q = select(Booking.priority, func.count(Booking.id).label("cnt")).group_by(Booking.priority)
    if day_start:
        q = q.where(Booking.received_at >= day_start, Booking.received_at < day_end)
    result = await db.execute(q)
    rows = result.all()
    total = sum(r.cnt for r in rows) or 1
    return [
        PrioritySlice(
            name=row.priority,
            value=round((row.cnt / total) * 100),
            color=PRIORITY_COLORS.get(row.priority, "#94a3b8"),
        )
        for row in rows
    ]


@router.get("/daily-summary", response_model=list[DailySummaryRow])
async def daily_summary(
    days: int = Query(7, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    end = date.today()
    start = end - timedelta(days=days - 1)

    result = await db.execute(
        select(
            func.date(Booking.received_at).label("day"),
            func.count(Booking.id).label("received"),
            func.sum(
                cast(case((Booking.status == "Completed", 1), else_=0), Integer)
            ).label("completed"),
        )
        .where(func.date(Booking.received_at) >= start)
        .group_by(func.date(Booking.received_at))
        .order_by(func.date(Booking.received_at).desc())
    )
    rows = result.all()
    summary = []
    for row in rows:
        received = row.received
        completed = row.completed or 0
        pending = received - completed
        rate = round((completed / received) * 100) if received else 0
        day_str = row.day.strftime("%d %b %Y") if hasattr(row.day, "strftime") else str(row.day)
        summary.append(DailySummaryRow(
            date=day_str,
            received=received,
            completed=completed,
            pending=pending,
            rate=rate,
        ))
    return summary


@router.get("/hourly", response_model=list[HourlyPoint])
async def hourly_activity(
    days: int = Query(30, ge=1, le=90),
    tz: str = Query("UTC"),
    date: str | None = Query(None),  # YYYY-MM-DD for single-day view
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Bookings received per local hour. Completed count is scoped to the same received window
    so chart totals match the bookings list exactly."""
    from datetime import datetime, timezone
    import zoneinfo

    if date:
        try:
            local_tz = zoneinfo.ZoneInfo(tz)
        except Exception:
            local_tz = zoneinfo.ZoneInfo("UTC")
        day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=local_tz)
        day_end = day_start + timedelta(days=1)
        recv_filter = (Booking.received_at >= day_start, Booking.received_at < day_end)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        recv_filter = (Booking.received_at >= since,)

    recv_hr = func.extract("hour", func.timezone(tz, Booking.received_at))

    result = await db.execute(
        select(
            recv_hr.label("hr"),
            func.count(Booking.id).label("received"),
            func.count(case((Booking.status == "Completed", 1))).label("completed"),
            func.count(case((Booking.status.in_(["Pending", "In Progress"]), 1))).label("open"),
        )
        .where(*recv_filter)
        .group_by(recv_hr)
        .order_by(recv_hr)
    )
    rows = result.all()
    received_map  = {int(r.hr): r.received  for r in rows}
    completed_map = {int(r.hr): r.completed for r in rows}
    open_map      = {int(r.hr): r.open      for r in rows}

    return [
        HourlyPoint(
            hour=h,
            label=f"{h:02d}:59" if h == 23 else f"{h:02d}:00",
            received=received_map.get(h, 0),
            completed=completed_map.get(h, 0),
            open=open_map.get(h, 0),
        )
        for h in range(24)
    ]


@router.get("/avg-completion", response_model=AvgCompletionReport)
async def avg_completion_time(
    date: Optional[str] = Query(None),
    tz: str = Query("UTC"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Average time (hours) from booking received to completed, overall and by priority."""
    day_start, day_end = _day_range(date, tz)
    epoch_diff = func.extract("epoch", Booking.completed_at - Booking.received_at) / 3600.0
    base_filter = [Booking.status == "Completed", Booking.completed_at.is_not(None)]
    if day_start:
        base_filter += [Booking.received_at >= day_start, Booking.received_at < day_end]

    overall_res = await db.execute(
        select(func.count(Booking.id).label("cnt"), func.avg(epoch_diff).label("avg_h"))
        .where(*base_filter)
    )
    overall = overall_res.one()

    by_priority_res = await db.execute(
        select(Booking.priority, func.count(Booking.id).label("cnt"), func.avg(epoch_diff).label("avg_h"))
        .where(*base_filter)
        .group_by(Booking.priority)
        .order_by(func.avg(epoch_diff))
    )
    by_priority = by_priority_res.all()

    from app.schemas.reports import AvgCompletionByPriority
    return AvgCompletionReport(
        overall_avg_hours=round(float(overall.avg_h or 0), 1),
        overall_count=overall.cnt or 0,
        by_priority=[
            AvgCompletionByPriority(priority=row.priority, avg_hours=round(float(row.avg_h or 0), 1), count=row.cnt)
            for row in by_priority
        ],
    )


@router.get("/status-breakdown", response_model=list[StatusBreakdownRow])
async def status_breakdown(
    date: Optional[str] = Query(None),
    tz: str = Query("UTC"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Count of Pending / In Progress / Completed grouped by priority."""
    day_start, day_end = _day_range(date, tz)
    q = select(Booking.priority, Booking.status, func.count(Booking.id).label("cnt")).group_by(Booking.priority, Booking.status)
    if day_start:
        q = q.where(Booking.received_at >= day_start, Booking.received_at < day_end)
    result = await db.execute(q)
    rows = result.all()

    data: dict[str, dict] = {}
    status_key = {"Pending": "pending", "In Progress": "in_progress", "Completed": "completed"}
    for row in rows:
        if row.priority not in data:
            data[row.priority] = {"priority": row.priority, "pending": 0, "in_progress": 0, "completed": 0}
        key = status_key.get(row.status)
        if key:
            data[row.priority][key] = row.cnt

    priority_order = ["Very Urgent", "Urgent", "Not Urgent", "Blank"]
    return [StatusBreakdownRow(**data[p]) for p in priority_order if p in data]
