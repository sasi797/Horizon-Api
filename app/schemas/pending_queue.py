from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PendingQueueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    booking_id: str
    reason: str
    pending_since: datetime


class AssignRequest(BaseModel):
    booking_id: str
    agent_id: UUID
