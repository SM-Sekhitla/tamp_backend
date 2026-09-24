from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.auth import Input

Cargo = Literal[
    "GENERAL_PALLETISED",
    "BULK_DRY",
    "BULK_LIQUID",
    "REFRIGERATED",
    "ABNORMAL",
    "CONTAINERISED",
    "LIVESTOCK",
    "HAZARDOUS",
]
Body = Literal[
    "TAUTLINER", "FLATBED", "TIPPER", "TANKER", "REFRIGERATED", "SIDE_TIPPER", "LOWBED", "DROPSIDE"
]


class Coordinates(Input):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)


class Place(Coordinates):
    label: str = Field(min_length=1, max_length=300)
    province: str = Field(max_length=30)


class Window(Input):
    from_: datetime = Field(alias="from")
    to: datetime

    @model_validator(mode="after")
    def order(self):
        if self.from_.tzinfo is None or self.to.tzinfo is None or self.from_ > self.to:
            raise ValueError("Use ordered, timezone-aware dates")
        return self


class Money(Input):
    amount: float = Field(ge=0, allow_inf_nan=False)
    currency: Literal["ZAR"]
    vat: Literal["incl", "excl"]


class LoadInput(Input):
    ownerId: str
    cargoType: Cargo
    weightKg: float = Field(gt=0, le=200000, allow_inf_nan=False)
    volumeM3: float | None = Field(default=None, gt=0, le=10000)
    origin: Place
    destination: Place
    distanceKm: float = Field(ge=0, le=30000, allow_inf_nan=False)
    pickupWindow: Window
    deliveryBy: datetime
    specialRequirements: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def dates(self):
        if self.deliveryBy.tzinfo is None or self.deliveryBy < self.pickupWindow.from_:
            raise ValueError("Delivery must follow pickup and include a timezone")
        return self


class Load(LoadInput):
    id: str = Field(min_length=1, max_length=80)
    reference: str
    status: Literal[
        "DRAFT", "POSTED", "MATCHED", "CONFIRMED", "COMPLETED", "CLOSED", "CANCELLED", "EXPIRED"
    ]
    createdAt: datetime
    requiredBodyTypes: list[Body]
    targetRate: Money | None = None


class Lane(Input):
    origin: str
    destination: str


class Truck(Input):
    id: str = Field(min_length=1, max_length=80)
    transporterId: str
    driverId: str | None = None
    registration: str = Field(min_length=1, max_length=40)
    bodyType: Body
    payloadCapacityKg: float = Field(gt=0, le=200000, allow_inf_nan=False)
    volumeCapacityM3: float | None = Field(default=None, gt=0)
    currentLocation: Place
    availableFrom: datetime
    availableTo: datetime
    preferredLanes: list[Lane] = Field(default_factory=list, max_length=50)
    status: Literal["AVAILABLE", "RESERVED", "ON_TRIP", "OFFLINE", "EXPIRED"]
    createdAt: datetime
    licenceExpiry: str | None = None
    photos: list[str] = Field(default_factory=list, max_length=20)
    documents: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def dates(self):
        if (
            self.availableFrom.tzinfo is None
            or self.availableTo.tzinfo is None
            or self.availableFrom > self.availableTo
        ):
            raise ValueError("Use ordered, timezone-aware availability dates")
        return self


class Match(Input):
    id: str = Field(min_length=1, max_length=80)
    loadId: str
    truckPostingId: str
    score: float = Field(ge=0, le=100)
    breakdown: list[dict]
    hardFilterResults: list[dict]
    status: Literal[
        "SUGGESTED", "OFFERED", "ACCEPTED", "CONFIRMED", "REJECTED", "WITHDRAWN", "EXPIRED"
    ]
    initiatedBy: Literal["FREIGHT_OWNER", "TRANSPORTER", "SYSTEM"]
    agreedRate: Money | None = None
    expiresAt: datetime
    createdAt: datetime
    confirmedByOwnerAt: str | None = None
    confirmedByTransporterAt: str | None = None
    rejectionReason: str | None = None


class Record(Input):
    id: str = Field(min_length=1, max_length=80)


class Rating(Record):
    tripId: str
    raterId: str
    rateeId: str
    stars: int = Field(ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=20)
    comment: str | None = Field(default=None, max_length=4000)
    createdAt: str


class Dispute(Record):
    tripId: str
    raisedById: str
    category: Literal["NON_ARRIVAL", "DAMAGE", "RATE_DISAGREEMENT", "DELAY", "CONDUCT", "OTHER"]
    description: str = Field(min_length=1, max_length=4000)
    status: Literal["OPEN", "UNDER_REVIEW", "RESOLVED", "DISMISSED"]
    resolutionNote: str | None = Field(default=None, max_length=4000)
    createdAt: str
    resolvedAt: str | None = None


class Reservation(Record):
    truckId: str
    loadId: str
    transporterId: str
    status: Literal["RESERVED", "RELEASED", "CONSUMED"]
    createdAt: str


class Fuel(Record):
    truckId: str
    transporterId: str
    driverId: str | None = None
    litres: float = Field(gt=0, le=5000)
    cost: Money
    odometerKm: float | None = Field(default=None, ge=0)
    station: str | None = None
    note: str | None = None
    filledAt: datetime
    createdAt: str


class Maintenance(Record):
    truckId: str
    transporterId: str
    driverId: str | None = None
    kind: Literal["SERVICE", "INSPECTION", "REPAIR", "TYRES", "LICENCE", "OTHER"]
    title: str = Field(min_length=1, max_length=300)
    dueDate: str
    status: Literal["SCHEDULED", "DONE"]
    cost: Money | None = None
    note: str | None = None
    completedAt: str | None = None
    createdAt: str


class ProofInput(Input):
    recipientName: str = Field(min_length=1, max_length=200)
    signature: str | None = Field(default=None, max_length=500000)
    photoName: str | None = Field(default=None, max_length=200)
    photoUrl: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=4000)


class Proof(ProofInput):
    id: str = Field(min_length=1, max_length=80)
    tripId: str
    loadId: str
    capturedAt: str


class Command(Input):
    loadId: str
    truckId: str | None = None
    proof: ProofInput | None = None


class Delta(Input):
    upserts: dict[str, list[dict]]
    deleteIds: dict[str, list[str]]

    @model_validator(mode="after")
    def bounded(self):
        allowed = {
            "loads",
            "trucks",
            "matches",
            "trips",
            "ratings",
            "disputes",
            "acceptances",
            "reservations",
            "audit",
            "fuelLogs",
            "maintenance",
            "proofs",
        }
        if (set(self.upserts) | set(self.deleteIds)) - allowed or sum(
            map(len, self.upserts.values())
        ) + sum(map(len, self.deleteIds.values())) > 500:
            raise ValueError("Invalid or oversized snapshot delta")
        return self


SCHEMAS = {
    "loads": Load,
    "trucks": Truck,
    "matches": Match,
    "ratings": Rating,
    "disputes": Dispute,
    "reservations": Reservation,
    "fuelLogs": Fuel,
    "maintenance": Maintenance,
    "proofs": Proof,
}


class CargoInput(Input):
    cargoType: Cargo


class Quote(CargoInput):
    distanceKm: float = Field(ge=0, le=30000, allow_inf_nan=False)
    weightKg: float = Field(gt=0, le=200000, allow_inf_nan=False)


class Distance(Input):
    a: Place
    b: Place


class Score(Input):
    load: Load
    trucks: list[Truck] = Field(max_length=500)
    operators: list[dict] = Field(max_length=500)
    owner: dict


class RouteInput(Input):
    origin: Coordinates
    destination: Coordinates


class Autocomplete(Input):
    query: str = Field(min_length=1, max_length=200)
    sessionToken: str | None = Field(default=None, max_length=100)


class Details(Input):
    placeId: str = Field(min_length=1, max_length=300)
    sessionToken: str | None = Field(default=None, max_length=100)


class Phone(Input):
    raw: str = Field(max_length=50)


class Limit(Input):
    limit: int = Field(default=50, ge=1, le=200)


class Read(Input):
    ids: list[str] | None = Field(default=None, max_length=200)


class Hook(Input):
    url: str = Field(max_length=2000)
    events: list[str] = Field(min_length=1, max_length=30)


class Id(Input):
    id: str


class Active(Id):
    active: bool


class TrackingLink(Input):
    loadId: str


class TrackingView(Input):
    token: str = Field(max_length=1000)
