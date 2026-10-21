"""Permanent fleet assets and auditable driver assignment history."""

from app.core.security import iso, new_id
from app.services.records import get, put, rows


def _vehicle_document(truck: dict, owner_id: str, existing: dict | None = None) -> dict:
    now = iso()
    return {
        "id": truck["vehicleId"],
        "ownerId": owner_id,
        "registration": truck["registration"],
        **({"vin": truck["vin"]} if truck.get("vin") else {}),
        **({"make": truck["make"]} if truck.get("make") else {}),
        **({"model": truck["model"]} if truck.get("model") else {}),
        **({"year": truck["year"]} if truck.get("year") else {}),
        "bodyType": truck["bodyType"],
        "payloadCapacityKg": truck["payloadCapacityKg"],
        **({"volumeCapacityM3": truck["volumeCapacityM3"]} if truck.get("volumeCapacityM3") else {}),
        **({"licenceExpiry": truck["licenceExpiry"]} if truck.get("licenceExpiry") else {}),
        "photos": truck.get("photos", []),
        "documents": truck.get("documents", []),
        "verification": (existing or {}).get("verification", "PENDING"),
        "status": (existing or {}).get("status", "ACTIVE"),
        "createdAt": (existing or {}).get("createdAt", now),
        "updatedAt": now,
    }


def sync_vehicle_for_truck(db, actor, truck: dict, old: dict | None = None) -> None:
    """Create/update the permanent asset and append driver changes atomically."""
    vehicle_id = truck.get("vehicleId") or (old or {}).get("vehicleId") or new_id("VH")
    truck["vehicleId"] = vehicle_id
    vehicle_row = get(db, "vehicles", vehicle_id, True)
    vehicle = vehicle_row.data if vehicle_row else None
    put(db, "vehicles", _vehicle_document(truck, truck["transporterId"], vehicle), truck["transporterId"])

    previous_driver = (old or {}).get("driverId")
    current_driver = truck.get("driverId")
    if previous_driver == current_driver:
        return
    ended = iso()
    for assignment in rows(db, "vehicleDriverAssignments"):
        if assignment.data["vehicleId"] == vehicle_id and assignment.data["status"] == "ACTIVE":
            put(db, "vehicleDriverAssignments", {**assignment.data, "status": "ENDED", "endsAt": ended}, assignment.owner_id)
    if current_driver:
        put(db, "vehicleDriverAssignments", {
            "id": new_id("VA"), "vehicleId": vehicle_id, "driverId": current_driver,
            "assignedBy": actor.id, "status": "ACTIVE", "startsAt": ended,
            "createdAt": ended,
        }, truck["transporterId"])
