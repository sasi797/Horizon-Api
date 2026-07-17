from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_admin
from app.models.role import Role
from app.models.user import User
from app.schemas.user import UserCreate, UserOut, UserRoleRef, UserUpdate
from app.utils.jwt import hash_password

router = APIRouter(prefix="/users", tags=["users"])


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        name=user.name,
        email=user.email,
        role_id=user.role_id,
        role=UserRoleRef.model_validate(user.role_obj),
        is_active=user.is_active,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.get("", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    rows = await db.execute(select(User).order_by(User.name))
    return [_user_out(u) for u in rows.scalars().all()]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="A user with that email already exists")

    role = await db.get(Role, body.role_id)
    if role is None:
        raise HTTPException(status_code=400, detail="Role not found")

    user = User(
        name=body.name,
        email=body.email,
        password_hash=hash_password(body.password),
        role_id=body.role_id,
        is_active=body.is_active,
    )
    db.add(user)
    await db.commit()
    result = await db.execute(select(User).where(User.id == user.id))
    return _user_out(result.scalar_one())


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = await db.get(User, UUID(user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    data = body.model_dump(exclude_unset=True)

    if "email" in data and data["email"] != user.email:
        dup = await db.execute(select(User).where(User.email == data["email"], User.id != user.id))
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="A user with that email already exists")

    if "role_id" in data:
        role = await db.get(Role, data["role_id"])
        if role is None:
            raise HTTPException(status_code=400, detail="Role not found")

    if data.get("is_active") is False and str(user.id) == str(current_user.id):
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")

    password = data.pop("password", None)
    for field, value in data.items():
        setattr(user, field, value)
    if password:
        user.password_hash = hash_password(password)

    await db.commit()
    db.expire(user, ["role_obj"])  # role_obj may be stale in the identity map if role_id changed
    result = await db.execute(select(User).where(User.id == user.id))
    return _user_out(result.scalar_one())


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if str(user_id) == str(current_user.id):
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")

    user = await db.get(User, UUID(user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = False
    await db.commit()
