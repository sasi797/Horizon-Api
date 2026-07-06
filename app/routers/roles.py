from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.role import Role
from app.models.user import User
from app.schemas.role import RoleCreate, RoleOut, RoleUpdate

router = APIRouter(prefix="/roles", tags=["roles"])


async def _role_with_count(db: AsyncSession, role_id: UUID) -> Role:
    row = await db.execute(
        select(Role, func.count(User.id).label("user_count"))
        .outerjoin(User, User.role_id == Role.id)
        .where(Role.id == role_id)
        .group_by(Role.id)
    )
    result = row.first()
    if result is None:
        raise HTTPException(status_code=404, detail="Role not found")
    role, count = result
    role.user_count = count
    return role


@router.get("", response_model=list[RoleOut])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    rows = await db.execute(
        select(Role, func.count(User.id).label("user_count"))
        .outerjoin(User, User.role_id == Role.id)
        .group_by(Role.id)
        .order_by(Role.name)
    )
    results = []
    for role, count in rows.all():
        role.user_count = count
        results.append(role)
    return results


@router.post("", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    existing = await db.execute(
        select(Role).where((Role.name == body.name) | (Role.key == body.key))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="A role with that name or key already exists")

    role = Role(name=body.name, key=body.key, permissions=body.permissions)
    db.add(role)
    await db.commit()
    role.user_count = 0
    return role


@router.put("/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: str,
    body: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    role = await db.get(Role, UUID(role_id))
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(role, field, value)
    await db.commit()
    return await _role_with_count(db, UUID(role_id))


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    role = await db.get(Role, UUID(role_id))
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")

    user_count = await db.scalar(select(func.count(User.id)).where(User.role_id == UUID(role_id)))
    if user_count and user_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete role — {user_count} user{'s are' if user_count != 1 else ' is'} still assigned to it"
        )

    await db.delete(role)
    await db.commit()
