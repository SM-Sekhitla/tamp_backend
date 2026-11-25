from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.asset_hire import Dimensions, Period
from app.schemas.auth import Input
from app.schemas.domain import Place

PassengerType = Literal["MINIBUS", "MIDIBUS", "BUS", "COACH"]


class PassengerVehicle(Input):
    id: str | None = Field(default=None, max_length=80)
    version: str | None = Field(default=None, max_length=80)
    type: PassengerType
    registration: str = Field(min_length=2, max_length=24, pattern=r"^[A-Za-z0-9 -]+$")
    make: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=80)
    seats: int = Field(ge=1, le=150)
    wheelchairPositions: int = Field(default=0, ge=0, le=30)
    displacedSeats: int = Field(default=0, ge=0, le=150)
    luggageKg: float = Field(default=0, ge=0, le=20000)
    luggageM3: float = Field(default=0, ge=0, le=150)
    luggageDimensions: Dimensions | None = None
    location: Place
    serviceArea: str = Field(min_length=1, max_length=200)
    availability: Period
    licenceExpiry: date
    permitExpiry: date
    photos: list[str] = Field(min_length=1, max_length=6)
    documents: dict[Literal["licence", "roadworthy", "operatingLicence", "insurance"], str]
    driverId: str | None = Field(default=None, max_length=80)
    published: bool = False

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.displacedSeats > self.seats or (not self.wheelchairPositions and self.displacedSeats):
            raise ValueError("Check seats removed in the wheelchair configuration")
        if self.documents.keys() != {"licence", "roadworthy", "operatingLicence", "insurance"}:
            raise ValueError("Upload the licence disc, roadworthy certificate, operating licence and insurance")
        return self


class Review(Input):
    id: str = Field(max_length=80)
    version: str = Field(max_length=80)
    status: Literal["VERIFIED", "REJECTED"]
    reason: str = Field(default="", max_length=500)
