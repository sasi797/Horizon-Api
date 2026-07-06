from typing import List, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Agent
from app.core.security import hash_password


def _make_initials(full_name: str) -> str:
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return full_name[:2].upper()


def _make_agent_code(seq: int) -> str:
    return f"AG-{seq:02d}"


async def get_agent_by_id(db: AsyncSession, agent_id: str) -> Optional[Agent]:
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.shift))
        .where(Agent.id == agent_id)
    )
    return result.scalar_one_or_none()


async def get_agent_by_email(db: AsyncSession, email: str) -> Optional[Agent]:
    from sqlalchemy import func
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.shift))
        .where(func.lower(Agent.email) == email.lower())
    )
    return result.scalar_one_or_none()


async def list_agents(
    db: AsyncSession,
    shift_id: Optional[UUID] = None,
    is_active: Optional[bool] = None,
) -> List[Agent]:
    q = select(Agent).options(selectinload(Agent.shift))
    if shift_id:
        q = q.where(Agent.shift_id == shift_id)
    if is_active is not None:
        q = q.where(Agent.is_active == is_active)
    q = q.order_by(Agent.shift_id, Agent.roster_position)
    result = await db.execute(q)
    return result.scalars().all()


async def create_agent(db: AsyncSession, data: dict) -> Agent:
    # Auto-generate agent code
    from sqlalchemy import func
    count_result = await db.execute(select(func.count()).select_from(Agent))
    count = count_result.scalar_one()
    code = _make_agent_code(count + 1)

    agent = Agent(
        agent_code=code,
        full_name=data["full_name"],
        initials=_make_initials(data["full_name"]),
        email=data["email"],
        password_hash=hash_password(data["password"]),
        role=data.get("role", "agent"),
        shift_id=data["shift_id"],
        roster_position=data.get("roster_position", 0),
        notify_by_email=data.get("notify_by_email", True),
    )
    db.add(agent)
    await db.flush()
    return agent


async def update_agent(db: AsyncSession, agent: Agent, data: dict) -> Agent:
    for field in ["full_name", "role", "shift_id", "roster_position", "is_active", "notify_by_email"]:
        if field in data and data[field] is not None:
            setattr(agent, field, data[field])
    if "full_name" in data:
        agent.initials = _make_initials(data["full_name"])
    await db.flush()
    return agent


async def reorder_roster(db: AsyncSession, shift_id: UUID, items: list) -> None:
    for item in items:
        await db.execute(
            update(Agent)
            .where(Agent.id == item["agent_id"], Agent.shift_id == shift_id)
            .values(roster_position=item["roster_position"])
        )
    await db.flush()
