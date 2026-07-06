from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AttachmentOut(BaseModel):
    id: UUID
    filename: str
    content_type: str
    size_bytes: int | None = None

    model_config = {"from_attributes": True}


class EmailMessageOut(BaseModel):
    id: UUID
    booking_id: str
    direction: str
    from_email: str
    to_email: str
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    cc_emails: str | None = None
    sent_at: datetime
    attachments: list[AttachmentOut] = []

    model_config = {"from_attributes": True}
