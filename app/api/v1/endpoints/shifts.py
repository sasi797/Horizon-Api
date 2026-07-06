from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone, time

from app.db.session import get_db
from app.core.security import get_current_agent, require_roles
from app.models.models import Shift
from app.schemas.schemas import ShiftOut, ShiftUpdate

router = APIRouter(prefix="/shifts", tags=["shifts"])


@router.get("", response_model=list[ShiftOut])
async def list_shifts(
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    result = await db.execute(select(Shift).order_by(Shift.start_time))
    return [ShiftOut.model_validate(s) for s in result.scalars()]


@router.get("/active", response_model=ShiftOut)
async def get_active_shift(
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    now_time = datetime.now(timezone.utc).time()
    result = await db.execute(select(Shift))
    shifts = result.scalars().all()

    for shift in shifts:
        if _is_shift_active(shift, now_time):
            return ShiftOut.model_validate(shift)

    # Fallback: return night shift
    result = await db.execute(select(Shift).where(Shift.shift_code == "SH-03"))
    return ShiftOut.model_validate(result.scalar_one())


def _is_shift_active(shift: Shift, now: time) -> bool:
    s, e = shift.start_time, shift.end_time
    if s < e:
        return s <= now < e
    # Overnight shift (e.g. 22:00 - 06:00)
    return now >= s or now < e


@router.patch("/{shift_id}", response_model=ShiftOut)
async def update_shift(
    shift_id: str,
    payload: ShiftUpdate,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(require_roles("admin")),
):
    result = await db.execute(select(Shift).where(Shift.id == shift_id))
    shift = result.scalar_one_or_none()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(shift, field, value)
    await db.flush()
    return ShiftOut.model_validate(shift)
