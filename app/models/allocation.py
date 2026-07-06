import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AllocationLog(Base):
    __tablename__ = "allocation_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[str] = mapped_column(String(25), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    pointer_value: Mapped[int] = mapped_column(Integer, nullable=False)
    pool_size: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    booking: Mapped["Booking"] = relationship("Booking", back_populates="allocation_logs")  # type: ignore[name-defined]
    agent: Mapped["Agent"] = relationship("Agent", back_populates="allocation_logs")  # type: ignore[name-defined]
