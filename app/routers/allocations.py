from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_current_user, get_db
from app.models.allocation import AllocationLog
from app.models.user import User
from app.schemas.allocation import AllocationLogOut

router = APIRouter(prefix="/allocations", tags=["allocations"])


@router.get("/log", response_model=list[AllocationLogOut])
async def allocation_log(
    booking_id: str | None = None,
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = (
        select(AllocationLog)
        .options(selectinload(AllocationLog.agent))
        .order_by(AllocationLog.allocated_at.asc())
        .offset(skip)
        .limit(limit)
    )
    if booking_id:
        q = q.where(AllocationLog.booking_id == booking_id)
    result = await db.execute(q)
    return result.scalars().all()
