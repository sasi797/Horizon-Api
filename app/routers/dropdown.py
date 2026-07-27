from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_current_user, get_db
from app.models.dropdown import Dropdown00, Dropdown01
from app.models.user import User

router = APIRouter(prefix="/dropdown", tags=["dropdown"])


class DropdownValueCreate(BaseModel):
    value: str
    label: str
    order_index: int = 0
    is_active: bool = True


class DropdownValueUpdate(BaseModel):
    value: str | None = None
    label: str | None = None
    order_index: int | None = None
    is_active: bool | None = None


class DropdownValueOut(BaseModel):
    id: UUID
    value: str
    label: str
    order_index: int
    is_active: bool

    class Config:
        from_attributes = True


class DropdownFieldCreate(BaseModel):
    module: str
    field_name: str
    field_label: str


class DropdownFieldUpdate(BaseModel):
    field_label: str | None = None
    is_active: bool | None = None


class DropdownFieldOut(BaseModel):
    id: UUID
    module: str
    field_name: str
    field_label: str
    is_active: bool
    values: list[DropdownValueOut] = []

    class Config:
        from_attributes = True


# --- dropdown00: module/field definitions -----------------------------------

@router.get("/fields", response_model=list[DropdownFieldOut])
async def list_fields(
    module: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Dropdown00).options(selectinload(Dropdown00.values)).order_by(Dropdown00.module, Dropdown00.field_name)
    if module:
        q = q.where(Dropdown00.module == module)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/fields/{field_id}", response_model=DropdownFieldOut)
async def get_field(
    field_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Dropdown00).options(selectinload(Dropdown00.values)).where(Dropdown00.id == field_id)
    )
    field = result.scalar_one_or_none()
    if not field:
        raise HTTPException(status_code=404, detail="Dropdown field not found")
    return field


@router.post("/fields", response_model=DropdownFieldOut)
async def create_field(
    body: DropdownFieldCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    field = Dropdown00(**body.model_dump())
    db.add(field)
    await db.commit()
    await db.refresh(field, attribute_names=["values"])
    return field


@router.put("/fields/{field_id}", response_model=DropdownFieldOut)
async def update_field(
    field_id: UUID,
    body: DropdownFieldUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    field = await db.get(Dropdown00, field_id)
    if not field:
        raise HTTPException(status_code=404, detail="Dropdown field not found")
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(field, key, val)
    await db.commit()
    await db.refresh(field, attribute_names=["values"])
    return field


@router.delete("/fields/{field_id}", status_code=204)
async def delete_field(
    field_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    field = await db.get(Dropdown00, field_id)
    if not field:
        raise HTTPException(status_code=404, detail="Dropdown field not found")
    await db.delete(field)
    await db.commit()


# --- dropdown01: values for a field ------------------------------------------

@router.post("/fields/{field_id}/values", response_model=DropdownValueOut)
async def create_value(
    field_id: UUID,
    body: DropdownValueCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    field = await db.get(Dropdown00, field_id)
    if not field:
        raise HTTPException(status_code=404, detail="Dropdown field not found")
    item = Dropdown01(dropdown00_id=field_id, **body.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@router.put("/values/{value_id}", response_model=DropdownValueOut)
async def update_value(
    value_id: UUID,
    body: DropdownValueUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    item = await db.get(Dropdown01, value_id)
    if not item:
        raise HTTPException(status_code=404, detail="Dropdown value not found")
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(item, key, val)
    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/values/{value_id}", status_code=204)
async def delete_value(
    value_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    item = await db.get(Dropdown01, value_id)
    if not item:
        raise HTTPException(status_code=404, detail="Dropdown value not found")
    await db.delete(item)
    await db.commit()


# --- convenience lookup used by dropdown UIs across the app -----------------

@router.get("/values", response_model=list[DropdownValueOut])
async def list_values_for_field(
    module: str,
    field_name: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Active values for one module+field, in display order — what dropdowns
    in the UI (e.g. manifest Account Number, Vehicle Size, Service Type)
    actually call to populate their options."""
    field_result = await db.execute(
        select(Dropdown00).where(Dropdown00.module == module, Dropdown00.field_name == field_name)
    )
    field = field_result.scalar_one_or_none()
    if not field:
        return []
    values_result = await db.execute(
        select(Dropdown01)
        .where(Dropdown01.dropdown00_id == field.id, Dropdown01.is_active.is_(True))
        .order_by(Dropdown01.order_index)
    )
    return values_result.scalars().all()
