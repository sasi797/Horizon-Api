from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: UUID
    title: str
    body: str
    type: str
    entity_id: str | None = None
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class LatestUnreadBooking(BaseModel):
    id: str
    subject: str


class NotificationsListOut(BaseModel):
    items: list[NotificationOut]
    unread_count: int
    unread_bookings: int = 0
    latest_unread_booking: LatestUnreadBooking | None = None
