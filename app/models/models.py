import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Text, Boolean, Integer, SmallInteger,
    Numeric, DateTime, Date, Time, ForeignKey, Enum as SAEnum,
    UniqueConstraint, CheckConstraint, JSON
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.session import Base

# ------------------------------------------------------------------
# Shifts
# ------------------------------------------------------------------

class Shift(Base):
    __tablename__ = "shifts"

    id                     = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shift_code             = Column(String(10), nullable=False, unique=True)
    name                   = Column(String(50), nullable=False)
    start_time             = Column(Time, nullable=False)
    end_time               = Column(Time, nullable=False)
    timezone               = Column(String(50), nullable=False, default="Asia/Kolkata")
    attendance_open_mins   = Column(SmallInteger, nullable=False, default=30)
    attendance_close_mins  = Column(SmallInteger, nullable=False, default=60)
    min_staffing_threshold = Column(SmallInteger, nullable=False, default=1)
    created_at             = Column(DateTime(timezone=True), server_default=func.now())

    agents             = relationship("Agent", back_populates="shift")
    attendance_records = relationship("AttendanceRecord", back_populates="shift")
    allocation_logs    = relationship("AllocationLog", back_populates="shift")

# ------------------------------------------------------------------
# Agents
# ------------------------------------------------------------------

class Agent(Base):
    __tablename__ = "agents"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_code      = Column(String(10), nullable=False, unique=True)
    full_name       = Column(String(255), nullable=False)
    initials        = Column(String(5), nullable=False)
    email           = Column(String(255), nullable=False, unique=True)
    password_hash   = Column(Text, nullable=False)
    role            = Column(SAEnum("agent", "supervisor", "admin", name="agent_role"), nullable=False, default="agent")
    shift_id        = Column(UUID(as_uuid=True), ForeignKey("shifts.id"), nullable=False)
    roster_position = Column(Integer, nullable=False, default=0)
    is_active       = Column(Boolean, nullable=False, default=True)
    notify_by_email = Column(Boolean, nullable=False, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    shift              = relationship("Shift", back_populates="agents")
    bookings           = relationship("Booking", back_populates="assigned_agent", foreign_keys="Booking.assigned_agent_id")
    attendance_records = relationship("AttendanceRecord", back_populates="agent", foreign_keys="AttendanceRecord.agent_id")
    allocation_logs    = relationship("AllocationLog", back_populates="agent")
    analyses           = relationship("BookingAnalysis", back_populates="agent")

# ------------------------------------------------------------------
# Booking ID Sequence
# ------------------------------------------------------------------

class BookingIdSequence(Base):
    __tablename__ = "booking_id_sequence"

    year          = Column(SmallInteger, primary_key=True)
    last_sequence = Column(Integer, nullable=False, default=0)

# ------------------------------------------------------------------
# Bookings
# ------------------------------------------------------------------

class Booking(Base):
    __tablename__ = "bookings"

    id                 = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id         = Column(String(20), nullable=False, unique=True)
    year               = Column(SmallInteger, nullable=False)
    sequence_number    = Column(Integer, nullable=False)
    sender_email       = Column(String(255), nullable=False)
    sender_name        = Column(String(255))
    email_subject      = Column(Text)
    email_body         = Column(Text)
    priority           = Column(SAEnum("urgent", "standard", "economy", name="booking_priority"), nullable=False, default="standard")
    status             = Column(SAEnum("received", "allocated", "in_review", "submitted", "confirmed", "cancelled", name="booking_status"), nullable=False, default="received")
    assigned_agent_id  = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    docket_number      = Column(String(100))
    transport_payload  = Column(JSONB)
    transport_response = Column(JSONB)
    sla_deadline_at    = Column(DateTime(timezone=True))
    received_at        = Column(DateTime(timezone=True), server_default=func.now())
    allocated_at       = Column(DateTime(timezone=True))
    submitted_at       = Column(DateTime(timezone=True))
    confirmed_at       = Column(DateTime(timezone=True))
    created_at         = Column(DateTime(timezone=True), server_default=func.now())
    updated_at         = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("year", "sequence_number"),)

    assigned_agent = relationship("Agent", back_populates="bookings", foreign_keys=[assigned_agent_id])
    attachments    = relationship("BookingAttachment", back_populates="booking", cascade="all, delete-orphan")
    analysis       = relationship("BookingAnalysis", back_populates="booking", uselist=False, cascade="all, delete-orphan")
    allocation_log = relationship("AllocationLog", back_populates="booking", uselist=False)

# ------------------------------------------------------------------
# Booking Attachments
# ------------------------------------------------------------------

class BookingAttachment(Base):
    __tablename__ = "booking_attachments"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id   = Column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    filename     = Column(String(255), nullable=False)
    storage_path = Column(Text, nullable=False)
    mime_type    = Column(String(100))
    size_bytes   = Column(Integer)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    booking = relationship("Booking", back_populates="attachments")

# ------------------------------------------------------------------
# Booking Analyses
# ------------------------------------------------------------------

class BookingAnalysis(Base):
    __tablename__ = "booking_analyses"

    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id           = Column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, unique=True)
    agent_id             = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    cargo_type           = Column(String(100))
    weight_kg            = Column(Numeric(10, 2))
    origin               = Column(Text)
    destination          = Column(Text)
    required_dispatch_at = Column(DateTime(timezone=True))
    vehicle_preference   = Column(String(100))
    handling_instructions = Column(String(100))
    agent_notes          = Column(Text)
    is_draft             = Column(Boolean, nullable=False, default=True)
    submitted_at         = Column(DateTime(timezone=True))
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    updated_at           = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    booking = relationship("Booking", back_populates="analysis")
    agent   = relationship("Agent", back_populates="analyses")

# ------------------------------------------------------------------
# Attendance Records (immutable append-only)
# ------------------------------------------------------------------

class AttendanceRecord(Base):
    __tablename__ = "attendance_records"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id      = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    shift_id      = Column(UUID(as_uuid=True), ForeignKey("shifts.id"), nullable=False)
    shift_date    = Column(Date, nullable=False)
    status        = Column(SAEnum("present", "absent", "late", "on_break", "left_early", name="attendance_status"), nullable=False)
    marked_by     = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    marked_at     = Column(DateTime(timezone=True), server_default=func.now())
    supersedes_id = Column(UUID(as_uuid=True), ForeignKey("attendance_records.id"), nullable=True)
    is_current    = Column(Boolean, nullable=False, default=True)
    requires_auth = Column(Boolean, nullable=False, default=False)

    agent    = relationship("Agent", back_populates="attendance_records", foreign_keys=[agent_id])
    shift    = relationship("Shift", back_populates="attendance_records")
    marker   = relationship("Agent", foreign_keys=[marked_by])

# ------------------------------------------------------------------
# Allocation State (single row)
# ------------------------------------------------------------------

class AllocationState(Base):
    __tablename__ = "allocation_state"

    id         = Column(Integer, primary_key=True, default=1)
    pointer    = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# ------------------------------------------------------------------
# Allocation Log (immutable)
# ------------------------------------------------------------------

class AllocationLog(Base):
    __tablename__ = "allocation_log"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id    = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    agent_id      = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    shift_id      = Column(UUID(as_uuid=True), ForeignKey("shifts.id"), nullable=False)
    allocated_at  = Column(DateTime(timezone=True), server_default=func.now())
    pointer_value = Column(Integer, nullable=False)
    pool_size     = Column(SmallInteger, nullable=False)
    method        = Column(SAEnum("round_robin", "manual_supervisor", name="allocation_method"), nullable=False, default="round_robin")
    manual_by     = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)

    booking    = relationship("Booking", back_populates="allocation_log")
    agent      = relationship("Agent", back_populates="allocation_logs", foreign_keys=[agent_id])
    shift      = relationship("Shift", back_populates="allocation_logs")
    supervisor = relationship("Agent", foreign_keys=[manual_by])

# ------------------------------------------------------------------
# Audit Log
# ------------------------------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_log"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(String(50), nullable=False)
    entity_id   = Column(UUID(as_uuid=True), nullable=False)
    event       = Column(String(100), nullable=False)
    actor_id    = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    payload     = Column(JSONB)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
