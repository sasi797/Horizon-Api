from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


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
    job_service_type: str | None
    packages: list[dict]
    extracted_data: dict
    source_kind: str
    blind_document_id: UUID | None
    blind_pdf_url: str | None = None
    status: str
    manifest_id: UUID | None
    manifest_sequence: int | None
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
    source_kind: str
    email_body_text: str | None


class HawbManifestDocumentOut(HawbDocumentOut):
    pdf_url: str


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
    job_service_type: str | None = None

    @field_validator("collection_at", "delivery_at")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


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
    status: str
    exported_at: datetime | None
    start_point: str | None
    end_point: str | None
    created_by: UUID | None
    created_by_name: str | None
    source_kind: str
    created_at: datetime


class HawbManifestDetailOut(HawbManifestOut):
    jobs: list[HawbJobOut]
    documents: list[HawbManifestDocumentOut]


class HawbJobPendingUpdateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: UUID
    reason: str
    proposed_data: dict
    status: str
    created_at: datetime
    resolved_at: datetime | None
    job: HawbJobOut
    source_document: HawbDocumentOut


class ManifestUpdate(BaseModel):
    start_point: str | None = None
    end_point: str | None = None


class ManifestReorder(BaseModel):
    job_ids: list[UUID]
