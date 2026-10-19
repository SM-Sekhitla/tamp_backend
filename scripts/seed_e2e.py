"""Reset and seed a dedicated TAMP browser-test database.

Refuses to run unless the database name contains ``test``.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import text

from app.controllers.auth import create_party
from app.core.config import settings
from app.core.security import iso, now
from app.db.base import Base
from app.db.session import AsyncSessionLocal, engine
from app.services.records import put

PASSWORD = "Testing-password-123"
ORIGIN = {"label": "Johannesburg Test Depot", "province": "GP", "lat": -26.2041, "lng": 28.0473}
DESTINATION = {"label": "Rosebank Test Hub", "province": "GP", "lat": -26.1467, "lng": 28.0419}


def seed(db):
    parties = {}
    for key, role in [
        ("owner", "FREIGHT_OWNER"),
        ("carrier", "TRANSPORTER"),
        ("driver", "DRIVER"),
        ("admin", "ADMIN"),
    ]:
        party = create_party(
            db,
            f"e2e-{key}@tamp.test",
            PASSWORD,
            role,
            f"E2E {key.title()}",
            "TAMP E2E Logistics" if key != "owner" else "TAMP E2E Freight",
        )
        party.data = {**party.data, "verification": "VERIFIED", "province": "GP"}
        parties[key] = party.id

    start = now() + timedelta(hours=1)
    end = start + timedelta(hours=8)
    load = {
        "id": "LD-E2E-001",
        "reference": "LD-E2E-001",
        "ownerId": parties["owner"],
        "cargoType": "GENERAL_PALLETISED",
        "weightKg": 12_000,
        "volumeM3": 42,
        "requiredBodyTypes": ["TAUTLINER"],
        "origin": ORIGIN,
        "destination": DESTINATION,
        "distanceKm": 9,
        "pickupWindow": {"from": start.isoformat(), "to": (start + timedelta(hours=2)).isoformat()},
        "deliveryBy": end.isoformat(),
        "targetRate": {"amount": 4850, "currency": "ZAR", "vat": "incl"},
        "status": "CONFIRMED",
        "createdAt": iso(),
    }
    truck = {
        "id": "TR-E2E-001",
        "transporterId": parties["carrier"],
        "driverId": parties["driver"],
        "registration": "E2E 001 GP",
        "bodyType": "TAUTLINER",
        "payloadCapacityKg": 18_000,
        "volumeCapacityM3": 60,
        "currentLocation": ORIGIN,
        "availableFrom": start.isoformat(),
        "availableTo": end.isoformat(),
        "status": "ON_TRIP",
        "createdAt": iso(),
        "photos": [],
        "documents": [],
    }
    match = {
        "id": "MT-E2E-001",
        "loadId": load["id"],
        "truckPostingId": truck["id"],
        "score": 98,
        "breakdown": [],
        "hardFilterResults": [],
        "status": "CONFIRMED",
        "initiatedBy": "SYSTEM",
        "agreedRate": load["targetRate"],
        "expiresAt": end.isoformat(),
        "createdAt": iso(),
        "confirmedByOwnerAt": iso(),
        "confirmedByTransporterAt": iso(),
    }
    trip = {
        "id": "TP-E2E-001",
        "matchId": match["id"],
        "status": "SCHEDULED",
        "events": [{"status": "SCHEDULED", "at": iso(), "byUserId": parties["carrier"]}],
        "simulatedPosition": None,
        "progressPct": 0,
        "etaAt": end.isoformat(),
        "geofences": {
            "PICKUP": {"inside": False},
            "DESTINATION": {"inside": False},
        },
        "geofenceEvents": [],
        "suggestedAction": None,
        "activeIncidentIds": [],
        "operationalStatus": None,
    }
    put(db, "loads", load, parties["owner"])
    put(db, "trucks", truck, parties["carrier"])
    put(db, "matches", match, parties["owner"])
    put(db, "trips", trip, parties["carrier"])


async def main():
    database_name = settings.database_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in database_name.lower():
        raise SystemExit("Refusing to seed: DATABASE_URL must name a dedicated test database")
    async with engine.begin() as connection:
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    async with AsyncSessionLocal.begin() as session:
        await session.run_sync(seed)
    await engine.dispose()
    print("Seeded owner, carrier, driver and admin accounts (password: Testing-password-123)")


if __name__ == "__main__":
    asyncio.run(main())
