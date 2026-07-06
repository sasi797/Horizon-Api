from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class RoleCreate(BaseModel):
    name: str
    key: str
    permissions: str


class RoleUpdate(BaseModel):
    name: str | None = None
    key: str | None = None
    permissions: str | None = None


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    key: str
    permissions: str
    user_count: int = 0
    created_at: datetime
    updated_at: datetime
