from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProcessedEmail(Base):
    """
    Tracks every IMAP Message-ID we have already handled.
    Intentionally has NO foreign key to bookings so that deleting a booking
    does not remove the record — preventing re-ingestion of the same email.
    """
    __tablename__ = "processed_emails"

    message_id: Mapped[str] = mapped_column(String(998), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
