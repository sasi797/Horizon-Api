from __future__ import annotations
from datetime import datetime, date, time
from decimal import Decimal
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field


# ------------------------------------------------------------------ #
#  Shared                                                             #
# ------------------------------------------------------------------ #

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


# ------------------------------------------------------------------ #
#  Shifts                                                             #
# ------------------------------------------------------------------ #

class ShiftOut(BaseModel):
    id: UUID
    shift_code: str
    name: str
    start_time: time
    end_time: time
    timezone: str
    attendance_open_mins: int
    attendance_close_mins: int
    min_staffing_threshold: int

    class Config:
        from_attributes = True


class ShiftUpdate(BaseModel):
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    timezone: Optional[str] = None
    attendance_open_mins: Optional[int] = None
    attendance_close_mins: Optional[int] = None
    min_staffing_threshold: Optional[int] = None


# ------------------------------------------------------------------ #
#  Agents                                                             #
# ------------------------------------------------------------------ #

class AgentCreate(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    role: str = "agent"
    shift_id: UUID
    roster_position: int = 0
    notify_by_email: bool = True


class AgentUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    shift_id: Optional[UUID] = None
    roster_position: Optional[int] = None
    is_active: Optional[bool] = None
    notify_by_email: Optional[bool] = None


class AgentOut(BaseModel):
    id: UUID
    agent_code: str
    full_name: str
    initials: str
    email: str
    role: str
    shift_id: UUID
    shift: Optional[ShiftOut] = None
    roster_position: int
    is_active: bool
    notify_by_email: bool
    created_at: datetime

    class Config:
        from_attributes = True


class RosterReorderItem(BaseModel):
    agent_id: UUID
    roster_position: int


class RosterReorderRequest(BaseModel):
    shift_id: UUID
    agents: List[RosterReorderItem]


# ------------------------------------------------------------------ #
#  Bookings                                                           #
# ------------------------------------------------------------------ #

class BookingAttachmentOut(BaseModel):
    id: UUID
    filename: str
    storage_path: str
    mime_type: Optional[str]
    size_bytes: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


class BookingIngest(BaseModel):
    """Posted by the email poller"""
    sender_email: EmailStr
    sender_name: Optional[str] = None
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    priority: str = "standard"
    attachments: List[dict] = []   # [{filename, storage_path, mime_type, size_bytes}]


class BookingOut(BaseModel):
    id: UUID
    booking_id: str
    sender_email: str
    sender_name: Optional[str]
    email_subject: Optional[str]
    email_body: Optional[str]
    priority: str
    status: str
    assigned_agent_id: Optional[UUID]
    assigned_agent: Optional[AgentOut] = None
    docket_number: Optional[str]
    sla_deadline_at: Optional[datetime]
    received_at: datetime
    allocated_at: Optional[datetime]
    submitted_at: Optional[datetime]
    confirmed_at: Optional[datetime]
    attachments: List[BookingAttachmentOut] = []

    class Config:
        from_attributes = True


class BookingListItem(BaseModel):
    id: UUID
    booking_id: str
    sender_email: str
    sender_name: Optional[str]
    email_subject: Optional[str]
    priority: str
    status: str
    assigned_agent_id: Optional[UUID]
    sla_deadline_at: Optional[datetime]
    received_at: datetime
    allocated_at: Optional[datetime]
    attachment_count: int = 0

    class Config:
        from_attributes = True


# ------------------------------------------------------------------ #
#  Analysis Form                                                      #
# ------------------------------------------------------------------ #

class AnalysisUpsert(BaseModel):
    cargo_type: Optional[str] = None
    weight_kg: Optional[Decimal] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    required_dispatch_at: Optional[datetime] = None
    vehicle_preference: Optional[str] = None
    handling_instructions: Optional[str] = None
    agent_notes: Optional[str] = None
    is_draft: bool = True  # False = submit


class AnalysisOut(BaseModel):
    id: UUID
    booking_id: UUID
    agent_id: UUID
    cargo_type: Optional[str]
    weight_kg: Optional[Decimal]
    origin: Optional[str]
    destination: Optional[str]
    required_dispatch_at: Optional[datetime]
    vehicle_preference: Optional[str]
    handling_instructions: Optional[str]
    agent_notes: Optional[str]
    is_draft: bool
    submitted_at: Optional[datetime]
    updated_at: datetime

    class Config:
        from_attributes = True


# ------------------------------------------------------------------ #
#  Transport Submission                                               #
# ------------------------------------------------------------------ #

class TransportSubmitResponse(BaseModel):
    booking_id: str
    docket_number: str
    status: str
    message: str


# ------------------------------------------------------------------ #
#  Attendance                                                         #
# ------------------------------------------------------------------ #

class AttendanceMarkRequest(BaseModel):
    """Agent self-marks present on login"""
    pass  # agent_id comes from JWT, shift_date is today


class AttendanceSupervisorMark(BaseModel):
    agent_id: UUID
    status: str   # present / absent / late / on_break / left_early
    shift_date: Optional[date] = None  # defaults to today


class AttendanceRecordOut(BaseModel):
    id: UUID
    agent_id: UUID
    agent: Optional[AgentOut] = None
    shift_id: UUID
    shift: Optional[ShiftOut] = None
    shift_date: date
    status: str
    marked_by: UUID
    marked_at: datetime
    is_current: bool
    requires_auth: bool

    class Config:
        from_attributes = True


class ShiftAttendanceSummary(BaseModel):
    shift: ShiftOut
    date: date
    agents: List[AttendanceRecordOut]
    present_count: int
    absent_count: int
    on_break_count: int
    coverage_pct: float


class DailyAttendanceSummary(BaseModel):
    date: date
    total_agents: int
    total_present: int
    total_absent: int
    coverage_pct: float
    shifts: List[ShiftAttendanceSummary]


# ------------------------------------------------------------------ #
#  Allocation                                                         #
# ------------------------------------------------------------------ #

class AllocationStateOut(BaseModel):
    pointer: int
    pool_size: int
    next_agent: Optional[AgentOut]
    active_pool: List[AgentOut]

    class Config:
        from_attributes = True


class AllocationLogOut(BaseModel):
    id: UUID
    booking_id: UUID
    booking: Optional[BookingListItem] = None
    agent_id: UUID
    agent: Optional[AgentOut] = None
    shift_id: UUID
    allocated_at: datetime
    pointer_value: int
    pool_size: int
    method: str

    class Config:
        from_attributes = True


class ManualAssignRequest(BaseModel):
    agent_id: UUID


# ------------------------------------------------------------------ #
#  Reports                                                            #
# ------------------------------------------------------------------ #

class BookingReportParams(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    priority: Optional[str] = None
    status: Optional[str] = None


class FairnessReportRow(BaseModel):
    agent: AgentOut
    total_bookings: int
    urgent_count: int
    standard_count: int
    economy_count: int


class SlaReportRow(BaseModel):
    priority: str
    total: int
    within_sla: int
    breached: int
    compliance_pct: float


# ------------------------------------------------------------------ #
#  Pagination                                                         #
# ------------------------------------------------------------------ #

class PaginatedResponse(BaseModel):
    items: list
    total: int
    page: int
    page_size: int
    total_pages: int
