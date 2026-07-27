import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Dropdown00(Base):
    """Defines a configurable dropdown field for a module, e.g. module='manifest', field_name='account_number'."""

    __tablename__ = "dropdown00"
    __table_args__ = (UniqueConstraint("module", "field_name", name="uq_dropdown00_module_field"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    module: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    field_label: Mapped[str] = mapped_column(String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    values: Mapped[list["Dropdown01"]] = relationship(back_populates="field", cascade="all, delete-orphan")


class Dropdown01(Base):
    """A selectable value for a Dropdown00 field."""

    __tablename__ = "dropdown01"
    __table_args__ = (UniqueConstraint("dropdown00_id", "value", name="uq_dropdown01_field_value"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dropdown00_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("dropdown00.id", ondelete="CASCADE"), nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(150), nullable=False)
    label: Mapped[str] = mapped_column(String(150), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    field: Mapped["Dropdown00"] = relationship(back_populates="values")
