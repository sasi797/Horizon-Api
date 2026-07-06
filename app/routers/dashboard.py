import json
import zoneinfo
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking
from app.models.user import User
from app.redis_client import get_redis
from app.schemas.reports import DashboardStats

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _da_count_expr():
    """Sum of individual DA numbers across all matching completed bookings."""
    return func.coalesce(
        func.sum(
            func.array_length(func.string_to_array(Booking.da_number, ','), 1)
        ),
        0,
    )


def _day_window(date_str: str, tz_str: str) -> tuple[datetime, datetime]:
    """Return UTC-aware (start, end) for a local calendar day."""
    try:
        local_tz = zoneinfo.ZoneInfo(tz_str)
    except Exception:
        local_tz = zoneinfo.ZoneInfo("UTC")
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=local_tz)
    return day_start, day_start + timedelta(days=1)


@router.get("/stats", response_model=DashboardStats)
async def dashboard_stats(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
    date: str | None = Query(None, description="Filter by received date (YYYY-MM-DD)"),
    tz: str = Query("UTC", description="IANA timezone for date interpretation"),
):
    sla_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    date_filters: tuple = ()
    if date:
        day_start, day_end = _day_window(date, tz)
        date_filters = (Booking.received_at >= day_start, Booking.received_at < day_end)

    # Only use cache for all-time (no date filter)
    if not date:
        cached = await redis.get("bts:dashboard:stats")
        if cached:
            return DashboardStats(**json.loads(cached))

    total       = await db.scalar(select(func.count(Booking.id)).where(*date_filters)) or 0
    pending     = await db.scalar(select(func.count(Booking.id)).where(Booking.status == "Pending", *date_filters)) or 0
    in_progress = await db.scalar(select(func.count(Booking.id)).where(Booking.status == "In Progress", *date_filters)) or 0
    completed   = await db.scalar(select(func.count(Booking.id)).where(Booking.status == "Completed", *date_filters)) or 0
    ignored     = await db.scalar(select(func.count(Booking.id)).where(Booking.status == "Ignored", *date_filters)) or 0
    at_risk     = await db.scalar(
        select(func.count(Booking.id)).where(
            Booking.status.in_(["Pending", "In Progress"]),
            Booking.received_at < sla_cutoff,
            *date_filters,
        )
    ) or 0
    da_count    = await db.scalar(
        select(_da_count_expr()).where(
            Booking.status == "Completed",
            Booking.da_number.isnot(None),
            Booking.da_number != '',
            *date_filters,
        )
    ) or 0

    stats = DashboardStats(total_bookings=total, pending=pending, in_progress=in_progress, completed=completed, ignored=ignored, da_numbers_count=int(da_count), at_risk=at_risk)
    if not date:
        await redis.setex("bts:dashboard:stats", 60, json.dumps(stats.model_dump()))
    return stats
