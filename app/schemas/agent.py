from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr


class AgentBase(BaseModel):
    name: str
    email: EmailStr
    shift_id: UUID | None = None


class AgentCreate(AgentBase):
    password: str
    role: str = "agent"


class AgentUpdate(BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    shift_id: UUID | None = None
    is_active: bool | None = None
    role: str | None = None


class ShiftInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    code: str


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    email: str
    shift_id: UUID | None
    shift: ShiftInfo | None = None
    is_active: bool = True
    role: str = "agent"
    created_at: datetime
    updated_at: datetime


class AgentWithStatus(AgentOut):
    status: str = "Present"
