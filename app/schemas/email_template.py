from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class EmailTemplateCreate(BaseModel):
    name: str
    body: str


class EmailTemplateUpdate(BaseModel):
    name: str | None = None
    body: str | None = None


class EmailTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    body: str
    created_at: datetime
    updated_at: datetime
