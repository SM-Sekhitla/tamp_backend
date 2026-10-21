from app.controllers.auth import find_email
from app.core.errors import require
from app.core.security import iso, new_id
from app.services.domain import audit, lock_domain
from app.services.records import needed, put, rows


def notify(db, party_id, trip_id, title, detail):
    put(db, "notifications", {"id": new_id("NT"), "title": title, "detail": detail, "at": iso(), "unread": True, "tone": "info", "subjectType": "TRIP", "subjectId": trip_id}, party_id)


def handle(action, data, db, request, response, party):
    require(action == "action", "Unknown dispatch action", 404)
    values = data.model_dump(mode="json", exclude_none=True)
    lock_domain(db)
    trip = needed(db, "trips", values["tripId"], True).data
    require(trip["status"] == "SCHEDULED", "Dispatch can only change before a trip starts", 409)
    match = needed(db, "matches", trip["matchId"]).data
    truck = needed(db, "trucks", match["truckPostingId"], True).data
    load = needed(db, "loads", match["loadId"]).data
    active = [
        r.data
        for r in rows(db, "dispatches")
        if r.data["tripId"] == trip["id"] and r.data["status"] in ["OFFERED", "ACCEPTED"]
    ]
    current = max(active, key=lambda item: item["offeredAt"], default=None)
    operation = values["action"]

    if operation == "ASSIGN":
        require(party.id == truck["transporterId"], "Only the carrier may dispatch this job", 403)
        require(values.get("driverEmail"), "Driver email is required", 400)
        driver = find_email(db, values["driverEmail"])
        owner_driver = (
            driver is not None
            and driver.id == party.id
            and party.id == truck["transporterId"]
            and driver.data["role"] == "TRANSPORTER"
        )
        require(
            driver is not None
            and (driver.data["role"] == "DRIVER" or owner_driver)
            and driver.data["verification"] == "VERIFIED",
            "Verified driver not found",
            404,
        )
        if current:
            put(db, "dispatches", {**current, "status": "CANCELLED", "cancelledAt": iso(), "cancelledById": party.id})
        status = "ACCEPTED" if owner_driver else "OFFERED"
        now = iso()
        dispatch = {"id": new_id("DP"), "tripId": trip["id"], "loadId": load["id"], "truckId": truck["id"], "carrierId": party.id, "driverId": driver.id, "status": status, "offeredAt": now, "respondedAt": now if owner_driver else None, "note": values.get("note"), "timeline": [{"status": status, "at": now, "byUserId": party.id}]}
        put(db, "dispatches", dispatch, party.id)
        updated_truck = {**truck, "driverId": driver.id}
        from app.services.fleet import sync_vehicle_for_truck
        sync_vehicle_for_truck(db, party, updated_truck, truck)
        put(db, "trucks", updated_truck)
        put(db, "trips", {**trip, "dispatchStatus": "ASSIGNED" if owner_driver else "AWAITING_DRIVER", "assignedDriverId": driver.id})
        if not owner_driver:
            notify(db, driver.id, trip["id"], "New delivery assignment", f"{load['origin']['label']} to {load['destination']['label']}")
    else:
        require(current is not None, "Active dispatch not found", 404)
        if operation in ["ACCEPT", "DECLINE"]:
            require(party.id == current["driverId"], "Only the assigned driver may respond", 403)
            status = "ACCEPTED" if operation == "ACCEPT" else "DECLINED"
            dispatch = {**current, "status": status, "respondedAt": iso(), "timeline": [*current.get("timeline", []), {"status": status, "at": iso(), "byUserId": party.id, "note": values.get("note")} ]}
            put(db, "dispatches", dispatch)
            put(db, "trips", {**trip, "dispatchStatus": "ASSIGNED" if status == "ACCEPTED" else "NEEDS_DRIVER", "assignedDriverId": party.id if status == "ACCEPTED" else None})
            if status == "DECLINED":
                updated_truck = {**truck, "driverId": None}
                from app.services.fleet import sync_vehicle_for_truck
                sync_vehicle_for_truck(db, party, updated_truck, truck)
                put(db, "trucks", updated_truck)
            notify(db, truck["transporterId"], trip["id"], f"Driver {status.lower()} assignment", load["id"])
        else:
            require(party.id == truck["transporterId"], "Only the carrier may cancel dispatch", 403)
            dispatch = {**current, "status": "CANCELLED", "cancelledAt": iso(), "cancelledById": party.id, "timeline": [*current.get("timeline", []), {"status": "CANCELLED", "at": iso(), "byUserId": party.id, "note": values.get("note")} ]}
            put(db, "dispatches", dispatch)
            updated_truck = {**truck, "driverId": None}
            from app.services.fleet import sync_vehicle_for_truck
            sync_vehicle_for_truck(db, party, updated_truck, truck)
            put(db, "trucks", updated_truck)
            put(db, "trips", {**trip, "dispatchStatus": "NEEDS_DRIVER", "assignedDriverId": None})
    audit(db, party, f"DISPATCH_{operation}", "TRIP", trip["id"], values.get("note") or operation.title())
    from app.services.messaging import conversation_for_trip, system_message

    conversation = conversation_for_trip(db, trip["id"])
    if conversation:
        conversation = {
            **conversation,
            "driverId": (
                dispatch["driverId"]
                if dispatch["status"] in ["OFFERED", "ACCEPTED"]
                else None
            ),
        }
        put(db, "conversations", conversation)
        system_message(
            db,
            conversation,
            f"Dispatch {operation.lower().replace('_', ' ')}: {dispatch['status'].lower()}.",
            "DISPATCH",
        )
    return {"ok": True, "dispatch": dispatch}
