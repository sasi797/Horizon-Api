from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TracePackage(BaseModel):
    supplier: str | None = None
    package_type: str | None = None
    weight_kg: float | None = None
    content_description: str | None = None
    temperature_range: str | None = None
    dimensions: str | None = None


class TraceAddress(BaseModel):
    name: str = Field(min_length=1, max_length=150, description="Company or site name")
    address: str = Field(min_length=1, max_length=200, description="Street / building lines; use \\n or ',' between lines")
    town: str = Field(min_length=1, max_length=100)
    postcode: str = Field(min_length=1, max_length=20)
    country: str | None = Field(None, max_length=60, description="e.g. United Kingdom")

    def to_blob(self) -> str:
        """The newline-separated block the rest of the app stores for
        shipper/consignee — mirrors Horizon-Web's buildAddress()."""
        lines = [self.name.strip()]
        lines += [s.strip() for s in self.address.split("\n") if s.strip()]
        lines.append(", ".join(p for p in (self.town.strip(), self.postcode.strip()) if p))
        if self.country and self.country.strip():
            lines.append(self.country.strip())
        return "\n".join(lines)


class TraceJobIn(BaseModel):
    hawb_number: str = Field(min_length=1, max_length=50)
    job_service_type: Literal["collection", "delivery", "collection_and_delivery"]
    shipper: TraceAddress
    consignee: TraceAddress
    collection_at: datetime
    delivery_at: datetime
    package_qty: int = Field(gt=0)
    weight_kg: float = Field(gt=0)
    dimensions: str = Field(min_length=1, max_length=100, description="e.g. 40x30x20")

    shipper_contact: str | None = Field(None, max_length=150)
    shipper_phone: str | None = Field(None, max_length=50)
    shipper_reference: str | None = Field(None, max_length=100)
    consignee_contact: str | None = Field(None, max_length=150)
    consignee_phone: str | None = Field(None, max_length=50)
    consignee_reference: str | None = Field(None, max_length=100)
    client_account: str | None = Field(None, max_length=50)
    dangerous_goods: bool = False
    dangerous_goods_notes: str | None = None
    temperature_range: str | None = Field(None, max_length=100)
    special_handling: str | None = None
    volumetric_weight_kg: float | None = None
    declared_value: float | None = None
    declared_value_currency: str | None = Field(None, max_length=10)
    direction: Literal["inbound", "outbound"] | None = None
    packages: list[TracePackage] = []

    @field_validator("hawb_number", "dimensions")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class TraceManifestIn(BaseModel):
    external_ref: str = Field(min_length=1, max_length=100, description="Trace/middleware's own manifest ID; makes retries idempotent")
    start_point: str | None = None
    end_point: str | None = None
    account_number: str | None = Field(None, max_length=50)
    vehicle_size: str | None = Field(None, max_length=30)
    customer_number: str | None = Field(None, max_length=20)
    jobs: list[TraceJobIn] = Field(min_length=1, max_length=200)

    @field_validator("external_ref")
    @classmethod
    def _strip_ref(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class TraceJobOut(BaseModel):
    hawb_number: str
    sequence: int


class TraceManifestOut(BaseModel):
    manifest_reference: str
    external_ref: str
    status: str
    job_count: int
    total_weight_kg: float
    jobs: list[TraceJobOut]
    already_existed: bool = False
