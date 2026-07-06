from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

AttendanceStatus = Literal["Present", "Absent", "On Break", "Late"]


class AttendanceUpsert(BaseModel):
    agent_id: UUID
    shift_id: UUID | None = None
    date: date
    status: AttendanceStatus
    check_in: datetime | None = None
    check_out: datetime | None = None


class AttendanceBulkUpdate(BaseModel):
    date: date
    shift_id: UUID | None = None
    records: list[AttendanceUpsert]


class AgentBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    email: str


class AttendanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    agent_id: UUID
    agent: AgentBrief | None = None
    shift_id: UUID | None
    date: date
    status: str
    check_in: datetime | None
    check_out: datetime | None
    updated_at: datetime


class AttendanceSummary(BaseModel):
    date: date
    present: int
    absent: int
    on_break: int
    late: int
    total: int
