from datetime import datetime, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ShiftCreate(BaseModel):
    name: str
    code: str
    start_time: time
    end_time: time


class ShiftUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    start_time: time | None = None
    end_time: time | None = None


class ShiftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    code: str
    start_time: time
    end_time: time
    created_at: datetime
    updated_at: datetime
