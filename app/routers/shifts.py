from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.shift import Shift
from app.models.user import User
from app.schemas.shift import ShiftCreate, ShiftOut, ShiftUpdate

router = APIRouter(prefix="/shifts", tags=["shifts"])


@router.get("", response_model=list[ShiftOut])
async def list_shifts(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Shift).order_by(Shift.start_time))
    return result.scalars().all()


@router.post("", response_model=ShiftOut, status_code=status.HTTP_201_CREATED)
async def create_shift(
    body: ShiftCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    existing = await db.execute(select(Shift).where(Shift.code == body.code))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Shift code already exists")
    shift = Shift(**body.model_dump())
    db.add(shift)
    await db.commit()
    await db.refresh(shift)
    return shift


@router.get("/{shift_id}", response_model=ShiftOut)
async def get_shift(
    shift_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    shift = await db.get(Shift, UUID(shift_id))
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")
    return shift


@router.put("/{shift_id}", response_model=ShiftOut)
async def update_shift(
    shift_id: str,
    body: ShiftUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    shift = await db.get(Shift, UUID(shift_id))
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(shift, field, value)
    await db.commit()
    await db.refresh(shift)
    return shift


@router.delete("/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shift(
    shift_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    shift = await db.get(Shift, UUID(shift_id))
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")
    await db.delete(shift)
    await db.commit()
