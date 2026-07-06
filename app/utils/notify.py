from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.models.role import Role
from app.models.user import User


async def notify_roles(
    db: AsyncSession,
    roles: list[str],
    title: str,
    body: str,
    ntype: str,
    entity_id: str | None = None,
) -> None:
    result = await db.execute(
        select(User)
        .join(Role, User.role_id == Role.id)
        .where(Role.key.in_(roles), User.is_active == True)
    )
    for user in result.scalars().all():
        db.add(Notification(user_id=user.id, title=title, body=body, type=ntype, entity_id=entity_id))


async def notify_user(
    db: AsyncSession,
    user_id,
    title: str,
    body: str,
    ntype: str,
    entity_id: str | None = None,
) -> None:
    db.add(Notification(user_id=user_id, title=title, body=body, type=ntype, entity_id=entity_id))
