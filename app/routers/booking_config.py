from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.booking_config import BookingConfig
from app.models.user import User

router = APIRouter(prefix="/booking-config", tags=["booking-config"])

VALID_TYPES = {"tag", "status", "priority"}


class ConfigItemCreate(BaseModel):
    type: str
    value: str
    label: str
    color: str = "gray"
    order_index: int = 0


class ConfigItemUpdate(BaseModel):
    value: str | None = None
    label: str | None = None
    color: str | None = None
    order_index: int | None = None


class ConfigItemOut(BaseModel):
    id: UUID
    type: str
    value: str
    label: str
    color: str
    order_index: int

    class Config:
        from_attributes = True


@router.get("", response_model=list[ConfigItemOut])
async def list_config(
    type: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(BookingConfig).order_by(BookingConfig.type, BookingConfig.order_index)
    if type:
        q = q.where(BookingConfig.type == type)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("", response_model=ConfigItemOut)
async def create_config(
    body: ConfigItemCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if body.type not in VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {VALID_TYPES}")
    item = BookingConfig(**body.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@router.put("/{item_id}", response_model=ConfigItemOut)
async def update_config(
    item_id: UUID,
    body: ConfigItemUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    item = await db.get(BookingConfig, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Config item not found")
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(item, field, val)
    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/{item_id}", status_code=204)
async def delete_config(
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    item = await db.get(BookingConfig, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Config item not found")
    await db.delete(item)
    await db.commit()
