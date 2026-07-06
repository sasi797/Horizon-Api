import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PendingQueue(Base):
    __tablename__ = "pending_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[str] = mapped_column(String(25), ForeignKey("bookings.id", ondelete="CASCADE"), unique=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    pending_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    booking: Mapped["Booking"] = relationship("Booking", back_populates="pending_queue")  # type: ignore[name-defined]
