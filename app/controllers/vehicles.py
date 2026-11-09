"""Platform vehicle compliance review actions."""

from app.core.errors import require
from app.core.security import iso
from app.services.rbac import require_permission
from app.services.records import get, put
from app.services.fleet import sync_vehicle_for_truck


def handle(action, data, db, request, response, party):
    del request, response
    require(action == "setVerification", "Vehicle action not found", 404)
    require_permission(db, party, "platform.admin")
    values = data.model_dump(mode="json")
    truck_row = get(db, "trucks", values["truckId"], True)
    require(truck_row is not None, "Truck not found", 404)
    truck = dict(truck_row.data)
    if not truck.get("vehicleId") or not get(db, "vehicles", truck.get("vehicleId"), True):
        sync_vehicle_for_truck(db, party, truck, truck_row.data)
        put(db, "trucks", truck, truck_row.owner_id)
    row = get(db, "vehicles", truck["vehicleId"], True)
    require(row is not None, "Vehicle record could not be created", 500)
    updated = {
        **row.data,
        "verification": values["status"],
        "verificationUpdatedAt": iso(),
        "verificationUpdatedBy": party.id,
    }
    put(db, "vehicles", updated, row.owner_id)
    return updated
