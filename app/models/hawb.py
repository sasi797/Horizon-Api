import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, FetchedValue, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class HawbProcessedEmail(Base):
    __tablename__ = "hawb_processed_emails"

    message_id: Mapped[str] = mapped_column(String(998), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HawbDocument(Base):
    __tablename__ = "hawb_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_message_id: Mapped[str | None] = mapped_column(String(998))
    sender_email: Mapped[str | None] = mapped_column(String(150))
    subject: Mapped[str | None] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(100), nullable=False, default="horizon-dev")
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processed")
    error_message: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="plain")
    email_body_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    jobs: Mapped[list["HawbJob"]] = relationship(
        "HawbJob", back_populates="document", cascade="all, delete-orphan", foreign_keys="HawbJob.document_id"
    )


class HawbManifest(Base):
    __tablename__ = "hawb_manifests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, server_default=FetchedValue())
    job_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_weight_kg: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="plain")
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_point: Mapped[str | None] = mapped_column(Text)
    end_point: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    jobs: Mapped[list["HawbJob"]] = relationship("HawbJob", back_populates="manifest")
    creator: Mapped["User | None"] = relationship("User", lazy="selectin")  # type: ignore[name-defined]

    @property
    def created_by_name(self) -> str | None:
        return self.creator.name if self.creator else None


class HawbJob(Base):
    __tablename__ = "hawb_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hawb_documents.id", ondelete="CASCADE"), nullable=False)
    blind_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("hawb_documents.id"))
    hawb_number: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    page_start: Mapped[int | None] = mapped_column(Integer)
    shipper: Mapped[str | None] = mapped_column(String(255))
    consignee: Mapped[str | None] = mapped_column(String(255))
    collection_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    package_qty: Mapped[int | None] = mapped_column(Integer)
    dangerous_goods: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dangerous_goods_notes: Mapped[str | None] = mapped_column(Text)
    weight_kg: Mapped[float | None] = mapped_column(Numeric(10, 2))
    client_account: Mapped[str | None] = mapped_column(String(50))
    package_sequence: Mapped[str | None] = mapped_column(String(20))
    shipper_contact: Mapped[str | None] = mapped_column(String(150))
    shipper_phone: Mapped[str | None] = mapped_column(String(50))
    shipper_reference: Mapped[str | None] = mapped_column(String(100))
    consignee_contact: Mapped[str | None] = mapped_column(String(150))
    consignee_phone: Mapped[str | None] = mapped_column(String(50))
    consignee_reference: Mapped[str | None] = mapped_column(String(100))
    temperature_range: Mapped[str | None] = mapped_column(String(100))
    dimensions: Mapped[str | None] = mapped_column(String(100))
    volumetric_weight_kg: Mapped[float | None] = mapped_column(Numeric(10, 2))
    declared_value: Mapped[float | None] = mapped_column(Numeric(10, 2))
    declared_value_currency: Mapped[str | None] = mapped_column(String(10))
    direction: Mapped[str | None] = mapped_column(String(20))
    special_handling: Mapped[str | None] = mapped_column(Text)
    job_service_type: Mapped[str | None] = mapped_column(String(30))
    packages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    extracted_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="plain")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending_review")
    manifest_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("hawb_manifests.id", ondelete="SET NULL"))
    manifest_sequence: Mapped[int | None] = mapped_column(Integer)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manifested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    document: Mapped["HawbDocument"] = relationship(
        "HawbDocument", back_populates="jobs", foreign_keys=[document_id], lazy="selectin"
    )
    blind_document: Mapped["HawbDocument | None"] = relationship(
        "HawbDocument", foreign_keys=[blind_document_id], lazy="selectin"
    )
    manifest: Mapped["HawbManifest | None"] = relationship("HawbManifest", back_populates="jobs")
