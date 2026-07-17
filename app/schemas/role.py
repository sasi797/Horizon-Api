from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class RoleCreate(BaseModel):
    name: str
    key: str
    permissions: str = Field(min_length=1)


class RoleUpdate(BaseModel):
    name: str | None = None
    key: str | None = None
    permissions: str | None = Field(default=None, min_length=1)


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    key: str
    permissions: str
    user_count: int = 0
    created_at: datetime
    updated_at: datetime
