from datetime import date
from decimal import Decimal
from uuid import UUID
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.asset_hire import Dimensions, Period
from app.schemas.auth import Input
from app.schemas.domain import Place

PassengerType = Literal["MINIBUS", "MIDIBUS", "BUS", "COACH"]

# Marketplace descriptions for the vehicle's primary configuration.
PASSENGER_SUBTYPES = {
    "MINIBUS": {
        "PASSENGER_VAN": "Standard passenger van",
        "COMMUTER_TAXI": "Commuter minibus taxi",
        "AIRPORT_SHUTTLE": "Airport shuttle",
        "STAFF_SHUTTLE": "Staff shuttle",
        "SCHOLAR_TRANSPORT": "Scholar transport",
        "TOURING_MINIBUS": "Touring minibus",
        "EXECUTIVE_SHUTTLE": "Executive shuttle",
        "WHEELCHAIR_ACCESSIBLE": "Wheelchair-accessible conversion",
        "HIGH_ROOF": "High-roof minibus",
        "LONG_WHEELBASE": "Long-wheelbase minibus",
        "OTHER": "Other minibus",
    },
    "MIDIBUS": {
        "STANDARD_MIDIBUS": "Standard midibus",
        "FEEDER_BUS": "Community or feeder bus",
        "AIRPORT_SHUTTLE": "Airport shuttle",
        "HOTEL_SHUTTLE": "Hotel or resort shuttle",
        "STAFF_SHUTTLE": "Staff shuttle",
        "SCHOLAR_TRANSPORT": "Scholar transport",
        "TOURING_MIDIBUS": "Touring midibus",
        "LOW_FLOOR": "Low-floor midibus",
        "WHEELCHAIR_ACCESSIBLE": "Wheelchair-accessible midibus",
        "SAFARI_BUS": "Safari or overland midibus",
        "OTHER": "Other midibus",
    },
    "BUS": {
        "CITY_BUS": "City bus",
        "COMMUTER_BUS": "Commuter bus",
        "FEEDER_BUS": "Regional or feeder bus",
        "BRT_STANDARD": "BRT bus",
        "BRT_ARTICULATED": "Articulated BRT bus",
        "ARTICULATED_BUS": "Articulated bus",
        "LOW_FLOOR": "Low-floor bus",
        "LOW_ENTRY": "Low-entry bus",
        "DOUBLE_DECKER": "Double-decker bus",
        "OPEN_TOP": "Open-top sightseeing bus",
        "SCHOOL_BUS": "School bus",
        "STAFF_BUS": "Staff or mine shuttle bus",
        "AIRPORT_APRON": "Airport apron bus",
        "OTHER": "Other bus",
    },
    "COACH": {
        "STANDARD_CHARTER": "Standard charter coach",
        "INTERCITY_COACH": "Intercity coach",
        "TOURING_COACH": "Touring coach",
        "SEMI_LUXURY": "Semi-luxury coach",
        "LUXURY_COACH": "Luxury coach",
        "EXECUTIVE_COACH": "Executive or VIP coach",
        "HIGH_DECK": "High-deck coach",
        "DOUBLE_DECKER": "Double-decker coach",
        "SLEEPER_COACH": "Sleeper or reclining-seat coach",
        "ACCESSIBLE_COACH": "Wheelchair-accessible coach",
        "OVERLAND_COACH": "Overland expedition coach",
        "OTHER": "Other coach",
    },
}



class PassengerVehicle(Input):
    id: str | None = Field(default=None, max_length=80)
    version: str | None = Field(default=None, max_length=80)
    type: PassengerType
    subtype: str | None = Field(default=None, max_length=40)
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
        if self.subtype is not None and self.subtype not in PASSENGER_SUBTYPES[self.type]:
            raise ValueError("Choose a subtype for the selected vehicle type")
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


class CharterLeg(Input):
    origin: Place
    destination: Place
    stops: list[Place] = Field(default_factory=list, max_length=6)
    pickupAt: AwareDatetime
    passengers: int = Field(ge=1, le=1000)
    wheelchairPositions: int = Field(default=0, ge=0, le=100)
    luggageKg: float = Field(default=0, ge=0, le=50000, allow_inf_nan=False)
    luggageM3: float = Field(default=0, ge=0, le=300, allow_inf_nan=False)
    largestBag: Dimensions | None = None
    routeToken: str = Field(min_length=1, max_length=500000)

    @model_validator(mode="after")
    def passenger_capacity(self):
        if self.wheelchairPositions > self.passengers:
            raise ValueError("Wheelchair passengers must be included in the passenger count")
        return self


class CharterSearch(Input):
    outbound: CharterLeg
    returnLeg: CharterLeg | None = None
    allowSplit: bool = False
    allowMultipleProviders: bool = False


class CharterRequest(CharterSearch):
    id: UUID
    vehicleId: str = Field(max_length=80)
    requireFleetMatch: bool = False
    contactName: str = Field(min_length=1, max_length=120)
    contactPhone: str = Field(min_length=5, max_length=40)
    notes: str = Field(default="", max_length=1500)


class CharterRecommendation(Period):
    id: str = Field(max_length=80)
    version: str = Field(max_length=80)


class CharterQuote(CharterRecommendation):
    vehicleIds: list[str] = Field(min_length=1, max_length=20)
    total: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    vat: Literal["INCL", "EXCL"]
    terms: str = Field(default="", max_length=1500)
    validUntil: AwareDatetime


class CharterAction(Input):
    id: str = Field(max_length=80)
    version: str = Field(max_length=80)
    action: Literal["CONFIRM", "DECLINE", "CANCEL"]
    reason: str = Field(default="", max_length=500)


class CharterGroupRequest(CharterRequest):
    vehicleIds: list[str] = Field(min_length=2, max_length=20)


class CharterGroupAction(Input):
    id: str = Field(max_length=80)
    versions: dict[str, str] = Field(min_length=2, max_length=20)
    action: Literal["CONFIRM", "CANCEL"]
    reason: str = Field(default="", max_length=500)


class CharterReplacement(Input):
    id: str = Field(max_length=80)
    version: str = Field(max_length=80)
    vehicleIds: list[str] = Field(default_factory=list, max_length=20)
