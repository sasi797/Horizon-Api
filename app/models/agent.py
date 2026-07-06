import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("shifts.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship("User", back_populates="agent")  # type: ignore[name-defined]
    shift: Mapped["Shift"] = relationship("Shift", back_populates="agents")  # type: ignore[name-defined]

    @property
    def role(self) -> str:
        return self.user.role if self.user else "agent"
    bookings: Mapped[list["Booking"]] = relationship("Booking", back_populates="agent")  # type: ignore[name-defined]
    attendance_records: Mapped[list["Attendance"]] = relationship("Attendance", back_populates="agent")  # type: ignore[name-defined]
    allocation_logs: Mapped[list["AllocationLog"]] = relationship("AllocationLog", back_populates="agent")  # type: ignore[name-defined]
