from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

Priority = str


class ParentBookingBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    subject: str


class ChildBookingBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    subject: str
    status: str
    da_number: str | None = None
    source_message_id: str | None = None


class BookingBase(BaseModel):
    subject: str
    priority: Priority = "Blank"
    sender_email: EmailStr


class BookingCreate(BookingBase):
    id: str | None = None
    parent_booking_id: str | None = None
    source_message_id: str | None = None


class BookingUpdate(BaseModel):
    subject: str | None = None
    priority: Priority | None = None
    status: str | None = None
    agent_id: UUID | None = None
    tags: str | None = None


class BookingStatusUpdate(BaseModel):
    status: str
    da_number: str | None = None
    da_description: str | None = None


class AgentBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    email: str


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    subject: str
    priority: str
    status: str
    agent_id: UUID | None
    agent: AgentBrief | None = None
    support_agents: list[AgentBrief] = []
    sender_email: str
    da_number: str | None
    da_description: str | None
    tags: str | None = None
    account_code: str | None = None
    received_at: datetime
    assigned_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    parent_booking_id: str | None = None
    parent_booking: ParentBookingBrief | None = None
    child_bookings: list[ChildBookingBrief] = []


class BookingListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    subject: str
    priority: str
    status: str
    agent: AgentBrief | None = None
    support_agents: list[AgentBrief] = []
    sender_email: str
    da_number: str | None = None
    da_description: str | None = None
    tags: str | None = None
    received_at: datetime
    assigned_at: datetime | None
    completed_at: datetime | None = None
    updated_at: datetime
    last_email_at: datetime
    is_read: bool = True
    has_reply: bool = False
    parent_booking_id: str | None = None
    has_children: bool = False


class BookingPageOut(BaseModel):
    items: list[BookingListOut]
    total: int
    page: int
    page_size: int
    total_pages: int


class BookingEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event: str
    actor_name: str | None
    old_value: str | None
    new_value: str | None
    created_at: datetime
