from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=10000, allow_inf_nan=False)


class Empty(Input):
    pass


class Email(Input):
    # Public authentication flows must support bootstrap identities using
    # reserved local domains. Registration still uses EmailStr below.
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")


class Login(Input):
    # Login also supports bootstrap identities using reserved local domains.
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=1, max_length=128)


class Business(Input):
    companyName: str = Field(min_length=1, max_length=200)
    registrationNumber: str = Field(max_length=100)
    businessType: str = Field(max_length=100)
    address: str = Field(max_length=1000)
    contactDetails: str = Field(max_length=300)
    vat: str = Field(max_length=100)


class DriverTruck(Input):
    registration: str = Field(min_length=1, max_length=40)
    bodyType: Literal[
        "TAUTLINER",
        "FLATBED",
        "TIPPER",
        "TANKER",
        "REFRIGERATED",
        "SIDE_TIPPER",
        "LOWBED",
        "DROPSIDE",
        "CONTAINER_SKELETAL",
        "BOX_TRUCK",
        "CRANE_TRUCK",
        "CAR_CARRIER",
        "LIVESTOCK_CARRIER",
    ]
    capacityT: float = Field(gt=0, le=200)


class Driver(Input):
    vehicleClasses: list[str] = Field(max_length=20)
    licenceCode: str = Field(max_length=30)
    prdp: bool
    workType: Literal["OWNER", "FLEET"]
    fleetName: str | None = Field(default=None, max_length=200)
    truck: DriverTruck | None = None


class Register(Login):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    code: str = Field(pattern=r"^\d{6}$")
    userType: Literal["CARGO_OWNER", "CARRIER", "BROKER", "DRIVER"]
    firstName: str = Field(min_length=1, max_length=100)
    lastName: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=1, max_length=40)
    province: str = Field(min_length=1, max_length=30)
    business: Business | None = None
    driver: Driver | None = None
    documents: list[str] = Field(default_factory=list, max_length=20)


class RegistrationDocument(Input):
    id: str | None = Field(default=None, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=300)
    contentType: str | None = Field(default=None, max_length=150)
    size: int | None = Field(default=None, ge=1)
    category: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, max_length=40)
    uploadedAt: str | None = Field(default=None, max_length=80)
    uploadedBy: str | None = Field(default=None, max_length=100)
    expiresAt: str | None = Field(default=None, max_length=80)
    reviewStatus: str | None = Field(default=None, max_length=40)
    reviewReason: str | None = Field(default=None, max_length=2000)
    reviewedAt: str | None = Field(default=None, max_length=80)
    reviewedBy: str | None = Field(default=None, max_length=100)


class RegistrationDocuments(Input):
    documents: list[RegistrationDocument] = Field(min_length=1, max_length=20)


class DocumentReview(Input):
    subjectType: Literal["REGISTRATION", "TRUCK", "DRIVER"]
    subjectId: str = Field(min_length=1, max_length=100)
    documentId: str = Field(min_length=1, max_length=300)
    status: Literal["APPROVED", "REJECTED"]
    reason: str | None = Field(default=None, max_length=2000)


class PasswordChange(Input):
    currentPassword: str = Field(max_length=128)
    newPassword: str = Field(min_length=8, max_length=128)


class PasswordReset(Email):
    code: str = Field(min_length=32, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    newPassword: str = Field(min_length=8, max_length=128)


class EmailChange(Input):
    newEmail: EmailStr


class Code(Input):
    code: str = Field(pattern=r"^\d{6}$")


class MfaVerify(Email):
    code: str = Field(pattern=r"^\d{6}$")


class SessionId(Input):
    sessionId: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class Profile(Input):
    contactName: str | None = Field(default=None, min_length=1, max_length=200)
    companyName: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    province: str | None = Field(default=None, max_length=30)
    avatarUrl: str | None = Field(default=None, max_length=300)


class Onboarding(Input):
    userType: Literal["CARGO_OWNER", "CARRIER", "BROKER", "DRIVER"]
    companyName: str = Field(min_length=1, max_length=200)
    phone: str = Field(max_length=40)
    province: str = Field(max_length=30)


class PartyId(Input):
    partyId: str


class OrganisationId(Input):
    organisationId: str


class OrganisationCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    type: Literal["CARGO_OWNER", "CARRIER", "BROKER", "DRIVER_NETWORK"]


class OrganisationMemberAdd(OrganisationId):
    email: EmailStr
    role: Literal["FREIGHT_OWNER", "TRANSPORTER", "DRIVER", "CARGO_OPERATOR", "DISPATCHER"]
    contactName: str | None = Field(default=None, min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    createAccount: bool = False


class OrganisationUpdate(OrganisationId):
    name: str = Field(min_length=1, max_length=200)


class OrganisationVerification(OrganisationId):
    status: Literal["PENDING", "VERIFIED", "REJECTED"]


class VehicleVerification(Input):
    truckId: str = Field(min_length=1, max_length=80)
    status: Literal["PENDING", "VERIFIED", "REJECTED"]


class OrganisationMemberRoles(OrganisationId):
    userId: str
    roles: list[Literal["FREIGHT_OWNER", "TRANSPORTER", "DRIVER", "CARGO_OPERATOR", "DISPATCHER"]] = Field(
        min_length=1, max_length=3
    )


class OrganisationMemberRemove(OrganisationId):
    userId: str


class Verification(PartyId):
    status: Literal["UNVERIFIED", "PENDING", "VERIFIED", "REJECTED"]


class Kyc(Input):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=300)
