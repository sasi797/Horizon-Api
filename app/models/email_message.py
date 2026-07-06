import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[str] = mapped_column(String(25), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(998))       # RFC 2822 Message-ID header
    in_reply_to: Mapped[str | None] = mapped_column(String(998))      # RFC 2822 In-Reply-To header
    conversation_id: Mapped[str | None] = mapped_column(String(200))  # Outlook conversationId
    graph_message_id: Mapped[str | None] = mapped_column(String(500)) # Graph API internal message ID (for reply threading)
    direction: Mapped[str] = mapped_column(String(10), nullable=False) # 'inbound' | 'outbound'
    from_email: Mapped[str] = mapped_column(String(150), nullable=False)
    to_email: Mapped[str] = mapped_column(Text, nullable=False)       # comma-separated
    cc_emails: Mapped[str | None] = mapped_column(Text)               # comma-separated
    subject: Mapped[str | None] = mapped_column(String(255))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    booking: Mapped["Booking"] = relationship("Booking", back_populates="email_messages")  # type: ignore[name-defined]
    attachments: Mapped[list["EmailAttachment"]] = relationship(
        "EmailAttachment", back_populates="message", cascade="all, delete-orphan"
    )


class EmailAttachment(Base):
    __tablename__ = "email_attachments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped["EmailMessage"] = relationship("EmailMessage", back_populates="attachments")
