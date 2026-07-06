import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class UploadResponse(BaseModel):
    id: uuid.UUID
    filename: str
    doc_type: str
    headers: list[str]
    rows: list[dict[str, Any]]

    model_config = {"from_attributes": True}


class HistoryItem(BaseModel):
    id: uuid.UUID
    filename: str
    doc_type: str
    uploaded_at: datetime
    uploaded_by: str

    model_config = {"from_attributes": True}


class HistoryDetail(BaseModel):
    id: uuid.UUID
    filename: str
    doc_type: str
    uploaded_at: datetime
    uploaded_by: str
    headers: list[str]
    rows: list[dict[str, Any]]

    model_config = {"from_attributes": True}
