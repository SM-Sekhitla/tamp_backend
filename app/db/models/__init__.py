from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

Json = JSON().with_variant(JSONB, "postgresql")


class Party(Base):
    __tablename__ = "parties"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    data: Mapped[dict] = mapped_column(Json)
    onboarding: Mapped[dict] = mapped_column(Json, default=dict)


class Session(Base):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    party_id: Mapped[str] = mapped_column(ForeignKey("parties.id", ondelete="CASCADE"), index=True)
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
    reported_by: Mapped[str] = mapped_column(ForeignKey("parties.id"), index=True)
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
    "notifications",
    "uploads",
    "webhooks",
    "deliveries",
]:
    attrs = {
        "__tablename__": name,
        "id": mapped_column(String(80), primary_key=True),
        "owner_id": mapped_column(ForeignKey("parties.id"), nullable=True, index=True),
        "data": mapped_column(Json, nullable=False),
    }
    refs = {
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
