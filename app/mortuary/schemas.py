from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ReservationKind(str, Enum):
    farewell_hall = "farewell_hall"
    cremator = "cremator"
    cold_storage = "cold_storage"
    vehicle = "vehicle"
    burial_team = "burial_team"


class CaseCreate(BaseModel):
    external_ref: str = Field(min_length=3, max_length=80)
    decedent_name: str = Field(min_length=1, max_length=120)
    identity_number: str | None = Field(default=None, max_length=80)
    death_time: datetime
    received_from: str = Field(min_length=2, max_length=160)
    family_contact: str = Field(min_length=2, max_length=120)
    family_phone: str = Field(min_length=5, max_length=40)
    special_notes: str = Field(default="", max_length=2000)


class CustodyTransferCreate(BaseModel):
    from_location: str = Field(min_length=2, max_length=120)
    to_location: str = Field(min_length=2, max_length=120)
    seal_code: str = Field(min_length=4, max_length=80)
    requested_by: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def locations_must_differ(self):
        if self.from_location == self.to_location:
            raise ValueError("交接起点与终点不能相同")
        return self


class CustodyAccept(BaseModel):
    accepted_by: str = Field(min_length=2, max_length=80)
    observed_seal_code: str = Field(min_length=4, max_length=80)
    condition_note: str = Field(default="", max_length=1000)


class ResourceCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    kind: ReservationKind
    site_code: str = Field(min_length=2, max_length=40)
    capacity: int = Field(default=1, ge=1, le=500)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ReservationCreate(BaseModel):
    resource_code: str = Field(min_length=2, max_length=40)
    case_id: int = Field(gt=0)
    start_at: datetime
    end_at: datetime
    purpose: str = Field(min_length=2, max_length=300)
    created_by: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end_at <= self.start_at:
            raise ValueError("结束时间必须晚于开始时间")
        if (self.end_at - self.start_at).total_seconds() > 259200:
            raise ValueError("单次预约不能超过七十二小时")
        return self


class ServiceOrderCreate(BaseModel):
    case_id: int = Field(gt=0)
    service_code: str = Field(min_length=2, max_length=60)
    quantity: int = Field(default=1, ge=1, le=100)
    unit_price_cents: int = Field(ge=0, le=100_000_000)
    requested_by: str = Field(min_length=2, max_length=80)
    notes: str = Field(default="", max_length=1000)


class BurialRightCreate(BaseModel):
    plot_code: str = Field(min_length=2, max_length=80)
    holder_name: str = Field(min_length=2, max_length=120)
    holder_identity: str = Field(min_length=4, max_length=80)
    starts_on: date
    expires_on: date
    case_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_period(self):
        if self.expires_on <= self.starts_on:
            raise ValueError("权属到期日必须晚于起始日")
        return self


class BurialRightRenew(BaseModel):
    years: int = Field(ge=1, le=20)
    handled_by: str = Field(min_length=2, max_length=80)
    payment_reference: str = Field(min_length=4, max_length=120)


class BurialRightStatusTarget(str, Enum):
    active = "active"
    suspended = "suspended"
    closed = "closed"


class BurialRightStatusChange(BaseModel):
    status: BurialRightStatusTarget
    reason: str = Field(min_length=2, max_length=500)
    handled_by: str = Field(min_length=2, max_length=80)


class InvoiceCreate(BaseModel):
    case_id: int = Field(gt=0)
    order_ids: list[int] = Field(min_length=1, max_length=100)
    created_by: str = Field(min_length=2, max_length=80)


class PaymentCreate(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000)
    channel: str = Field(min_length=2, max_length=40)
    external_reference: str = Field(min_length=4, max_length=120)
    received_by: str = Field(min_length=2, max_length=80)
