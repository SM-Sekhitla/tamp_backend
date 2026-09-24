from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=10000, allow_inf_nan=False)


class Empty(Input):
    pass


class Email(Input):
    email: EmailStr


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


class PasswordChange(Input):
    currentPassword: str = Field(max_length=128)
    newPassword: str = Field(min_length=8, max_length=128)


class PasswordReset(Email):
    code: str = Field(pattern=r"^\d{6}$")
    newPassword: str = Field(min_length=8, max_length=128)


class EmailChange(Input):
    newEmail: EmailStr


class Code(Input):
    code: str = Field(pattern=r"^\d{6}$")


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


class Verification(PartyId):
    status: Literal["UNVERIFIED", "PENDING", "VERIFIED", "REJECTED"]


class Kyc(Input):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=300)
