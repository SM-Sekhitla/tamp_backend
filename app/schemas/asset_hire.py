from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.auth import Input
from app.schemas.domain import Place, Window

AssetType = Literal["ADT", "MINE_DUMP_TRUCK", "EXCAVATOR", "FEL", "CRUSHER", "SCREEN", "TLB"]


class Dimensions(Input):
    lengthM: float = Field(gt=0, le=100)
    widthM: float = Field(gt=0, le=30)
    heightM: float = Field(gt=0, le=30)


class Period(Input):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def ordered(self):
        if self.start.utcoffset() is None or self.end.utcoffset() is None or self.end <= self.start:
            raise ValueError("Choose a valid start and end time")
        return self


class Asset(Input):
    id: str | None = Field(default=None, max_length=80)
    type: AssetType
    make: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=80)
    location: Place
    availability: Period
    blockedPeriods: list[Period] = Field(default_factory=list, max_length=50)
    published: bool = False
    photos: list[str] = Field(default_factory=list, max_length=6)
    specifications: str = Field(default="", max_length=1000)
    ratedPayloadTonnes: float | None = Field(default=None, gt=0, le=1000)
    transportWeightKg: float = Field(gt=0, le=600000)
    dimensions: Dimensions
    rate: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    unit: Literal["HOUR", "SHIFT", "DAY"]
    shiftHours: int = Field(default=8, ge=1, le=24)
    minimumUnits: int = Field(default=1, ge=1, le=1000)
    vat: Literal["incl", "excl"]
    operatorIncluded: bool = False
    fuelIncluded: bool = False

    @model_validator(mode="after")
    def payload(self):
        if self.type in {"ADT", "MINE_DUMP_TRUCK"} and self.ratedPayloadTonnes is None:
            raise ValueError("Enter manufacturer-rated payload in metric tonnes")
        return self


class Booking(Period):
    assetId: str = Field(max_length=80)
    site: Place
    listingVersion: str = Field(max_length=80)
    notes: str = Field(default="", max_length=2000)


class Action(Input):
    id: str = Field(max_length=80)
    action: Literal["CONFIRM", "DECLINE", "CANCEL", "START", "COMPLETE"]
    reason: str = Field(default="", max_length=1000)


class Handover(Input):
    id: str = Field(max_length=80)
    stage: Literal["DELIVERY", "RETURN"]
    condition: Literal["GOOD", "DAMAGE_FOUND"]
    meterHours: float | None = Field(default=None, ge=0, le=1000000)
    checks: dict[Literal["assetIdentity", "conditionInspected", "attachmentsChecked"], bool]
    notes: str = Field(default="", max_length=2000)
    photos: list[str] = Field(min_length=1, max_length=6)


class RentalInvoice(Input):
    id: str = Field(max_length=80)


class RentalPayment(RentalInvoice):
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    reference: str = Field(min_length=1, max_length=120)


class Move(Input):
    assetId: str = Field(max_length=80)
    bookingId: str | None = Field(default=None, max_length=80)
    origin: Place
    destination: Place
    pickupWindow: Window
    deliveryBy: datetime
    notes: str = Field(default="", max_length=2000)
    direction: Literal["DELIVERY", "COLLECTION"] = "DELIVERY"
