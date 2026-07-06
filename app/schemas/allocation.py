from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RunAllocationRequest(BaseModel):
    booking_id: str


class AllocationStatus(BaseModel):
    pointer: int
    pool_size: int
    next_agent_id: UUID | None
    next_agent_name: str | None


class AgentBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    email: str


class AllocationLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    booking_id: str
    agent_id: UUID
    agent: AgentBrief | None = None
    pointer_value: int
    pool_size: int
    allocated_at: datetime
