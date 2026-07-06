from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class HawbJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    hawb_number: str
    page_start: int | None
    shipper: str | None
    consignee: str | None
    collection_at: datetime | None
    delivery_at: datetime | None
    package_qty: int | None
    weight_kg: float | None
    dangerous_goods: bool
    dangerous_goods_notes: str | None
    client_account: str | None
    package_sequence: str | None
    shipper_contact: str | None
    shipper_phone: str | None
    shipper_reference: str | None
    consignee_contact: str | None
    consignee_phone: str | None
    consignee_reference: str | None
    temperature_range: str | None
    dimensions: str | None
    volumetric_weight_kg: float | None
    declared_value: float | None
    declared_value_currency: str | None
    direction: str | None
    special_handling: str | None
    packages: list[dict]
    extracted_data: dict
    status: str
    manifest_id: UUID | None
    locked: bool
    ready_at: datetime | None
    manifested_at: datetime | None
    created_at: datetime
    updated_at: datetime


class HawbDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    sender_email: str | None
    subject: str | None
    received_at: datetime
    job_count: int
    status: str
    error_message: str | None


class HawbJobDetailOut(HawbJobOut):
    document: HawbDocumentOut
    pdf_url: str


class HawbJobUpdate(BaseModel):
    shipper: str | None = None
    consignee: str | None = None
    collection_at: datetime | None = None
    delivery_at: datetime | None = None
    package_qty: int | None = None
    weight_kg: float | None = None
    dangerous_goods: bool | None = None
    dangerous_goods_notes: str | None = None
    client_account: str | None = None
    package_sequence: str | None = None
    shipper_contact: str | None = None
    shipper_phone: str | None = None
    shipper_reference: str | None = None
    consignee_contact: str | None = None
    consignee_phone: str | None = None
    consignee_reference: str | None = None
    temperature_range: str | None = None
    dimensions: str | None = None
    volumetric_weight_kg: float | None = None
    declared_value: float | None = None
    declared_value_currency: str | None = None
    direction: str | None = None
    special_handling: str | None = None


class HawbJobPageOut(BaseModel):
    items: list[HawbJobOut]
    total: int
    page: int
    page_size: int
    total_pages: int


class HawbManifestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reference_number: str
    job_count: int
    total_weight_kg: float
    created_by: UUID
    created_at: datetime


class HawbManifestDetailOut(HawbManifestOut):
    jobs: list[HawbJobOut]


class ManifestCreate(BaseModel):
    job_ids: list[UUID]
