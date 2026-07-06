# agents.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import get_current_agent, require_roles
from app.services import agent_service
from app.schemas.schemas import AgentCreate, AgentUpdate, AgentOut, RosterReorderRequest

router = APIRouter(prefix="/agents", tags=["agents"])

@router.get("", response_model=list[AgentOut])
async def list_agents(
    shift_id: str = None,
    is_active: bool = None,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    from uuid import UUID
    agents = await agent_service.list_agents(
        db,
        shift_id=UUID(shift_id) if shift_id else None,
        is_active=is_active,
    )
    return [AgentOut.model_validate(a) for a in agents]


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    agent = await agent_service.get_agent_by_id(db, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return AgentOut.model_validate(agent)


@router.post("", response_model=AgentOut, status_code=201)
async def create_agent(
    payload: AgentCreate,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(require_roles("admin")),
):
    agent = await agent_service.create_agent(db, payload.model_dump())
    return AgentOut.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(require_roles("supervisor", "admin")),
):
    agent = await agent_service.get_agent_by_id(db, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    updated = await agent_service.update_agent(db, agent, payload.model_dump(exclude_none=True))
    return AgentOut.model_validate(updated)


@router.patch("/roster/reorder", status_code=204)
async def reorder_roster(
    payload: RosterReorderRequest,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(require_roles("supervisor", "admin")),
):
    items = [{"agent_id": i.agent_id, "roster_position": i.roster_position} for i in payload.agents]
    await agent_service.reorder_roster(db, payload.shift_id, items)
