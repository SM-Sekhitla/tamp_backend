from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

Json = JSON().with_variant(JSONB, "postgresql")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    data: Mapped[dict] = mapped_column(Json)
    onboarding: Mapped[dict] = mapped_column(Json, default=dict)


# Compatibility alias while the API's public `Party` name is migrated separately.
Party = User


class Organisation(Base):
    __tablename__ = "organisations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    verification: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    data: Mapped[dict] = mapped_column(Json, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(20), index=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    system: Mapped[bool] = mapped_column(Boolean, default=True)


class Permission(Base):
    __tablename__ = "permissions"
    code: Mapped[str] = mapped_column(String(100), primary_key=True)
    description: Mapped[str] = mapped_column(String(300))


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_code: Mapped[str] = mapped_column(
        ForeignKey("permissions.code", ondelete="CASCADE"), primary_key=True
    )


class OrganisationMembership(Base):
    __tablename__ = "organisation_memberships"
    __table_args__ = (UniqueConstraint("organisation_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    organisation_id: Mapped[str] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", "organisation_id"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), index=True
    )
    organisation_id: Mapped[str | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    assigned_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    active_organisation_id: Mapped[str | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class EmailCode(Base):
    __tablename__ = "email_codes"
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    target: Mapped[str] = mapped_column(String(254))


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class VehiclePosition(Base):
    __tablename__ = "vehicle_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    truck_id: Mapped[str] = mapped_column(ForeignKey("trucks.id", ondelete="CASCADE"), index=True)
    reported_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_kph: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)


# Separate domain tables retain relational ownership/references; JSONB stores
# the nested camelCase wire document (places, score breakdowns, proof, etc.).
MODELS = {}
for name in [
    "loads",
    "vehicles",
    "vehicleDriverAssignments",
    "trucks",
    "matches",
    "route_quotes",
    "match_offers",
    "trips",
    "ratings",
    "disputes",
    "acceptances",
    "reservations",
    "audit",
    "fuelLogs",
    "maintenance",
    "proofs",
    "incidents",
    "invoices",
    "transactions",
    "dispatches",
    "conversations",
    "messages",
    "notifications",
    "uploads",
    "webhooks",
    "deliveries",
]:
    attrs = {
        "__tablename__": name,
        "id": mapped_column(String(80), primary_key=True),
        "owner_id": mapped_column(ForeignKey("users.id"), nullable=True, index=True),
        "organisation_id": mapped_column(
            ForeignKey("organisations.id"), nullable=True, index=True
        ),
        "data": mapped_column(Json, nullable=False),
    }
    refs = {
        "vehicleDriverAssignments": {"vehicle_id": "vehicles", "driver_id": "users"},
        "trucks": {"vehicle_id": "vehicles"},
        "matches": {"load_id": "loads", "truck_id": "trucks"},
        "route_quotes": {"load_id": "loads", "truck_id": "trucks", "match_id": "matches"},
        "match_offers": {"match_id": "matches"},
        "trips": {"match_id": "matches"},
        "ratings": {"trip_id": "trips"},
        "disputes": {"trip_id": "trips"},
        "acceptances": {"match_id": "matches"},
        "reservations": {"load_id": "loads", "truck_id": "trucks"},
        "proofs": {"load_id": "loads", "trip_id": "trips"},
        "incidents": {"load_id": "loads", "trip_id": "trips"},
        "invoices": {"load_id": "loads", "trip_id": "trips", "match_id": "matches"},
        "transactions": {"invoice_id": "invoices"},
        "dispatches": {"trip_id": "trips", "truck_id": "trucks"},
        "conversations": {"trip_id": "trips"},
        "messages": {"conversation_id": "conversations"},
        "fuelLogs": {"truck_id": "trucks"},
        "maintenance": {"truck_id": "trucks"},
    }
    for column, target in refs.get(name, {}).items():
        attrs[column] = mapped_column(ForeignKey(target + ".id"), nullable=False, index=True)
    MODELS[name] = type(name[0].upper() + name[1:], (Base,), attrs)
Index(
    "one_live_match_per_load",
    MODELS["matches"].load_id,
    unique=True,
    postgresql_where=text("data->>'status' IN ('ACCEPTED', 'CONFIRMED')"),
)
Index(
    "one_offer_revision_per_match",
    MODELS["match_offers"].match_id,
    text("(data->>'revision')"),
    unique=True,
)
Index("one_active_trip_per_truck_match", MODELS["trips"].match_id, unique=True)
Index(
    "one_reservation_per_load",
    MODELS["reservations"].load_id,
    unique=True,
    postgresql_where=text("data->>'status' = 'RESERVED'"),
)
Index(
    "one_rating_per_party_trip", MODELS["ratings"].owner_id, MODELS["ratings"].trip_id, unique=True
)
