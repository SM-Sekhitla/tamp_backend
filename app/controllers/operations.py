from datetime import timedelta

from app.api.deps import is_admin
from app.core.errors import require
from app.core.security import iso, now
from app.db.models import Party
from app.services.domain import audit, lock_domain
from app.services.records import needed, put, rows


def _notify(db, party_id, load_id, title, detail):
    from app.core.security import new_id

    put(
        db,
        "notifications",
        {
            "id": new_id("NT"),
            "title": title,
            "detail": detail,
            "at": iso(),
            "unread": True,
            "tone": "info",
            "subjectType": "LOAD",
            "subjectId": load_id,
        },
        party_id,
    )


def handle(action, data, db, request, response, party):
    require(action == "incidentAction", "Unknown operations action", 404)
    require(is_admin(party), "Administrator access is required", 403)
    lock_domain(db)
    values = data.model_dump(mode="json", exclude_none=True)
    incident = needed(db, "incidents", values["incidentId"], True).data
    trip = needed(db, "trips", incident["tripId"], True).data
    load = needed(db, "loads", incident["loadId"], True).data
    truck = needed(db, "trucks", incident["truckId"], True).data
    operation = values["action"]
    current = incident["status"]
    require(current != "RESOLVED" or operation in ["RESUME_TRIP", "REOPEN_LOAD"], "Incident is already resolved", 409)

    if operation == "ACKNOWLEDGE":
        require(current == "OPEN", "Only open incidents can be acknowledged", 409)
        incident = {**incident, "status": "ACKNOWLEDGED", "acknowledgedAt": iso(), "acknowledgedById": party.id}
    elif operation == "ASSIGN":
        assignee = values.get("assigneeId") or party.id
        assignee_party = db.get(Party, assignee)
        require(assignee_party is not None and is_admin(assignee_party), "Assignee must be an administrator", 400)
        incident = {**incident, "status": "ACKNOWLEDGED" if current == "OPEN" else current, "assignedToId": assignee, "assignedAt": iso()}
    elif operation == "BEGIN_RESOLUTION":
        require(current in ["OPEN", "ACKNOWLEDGED"], "Incident cannot enter resolution", 409)
        incident = {**incident, "status": "RESOLVING", "assignedToId": incident.get("assignedToId", party.id), "resolutionStartedAt": iso()}
    elif operation == "UPDATE_ETA":
        require(values.get("etaMinutes"), "ETA adjustment is required", 400)
        put(db, "trips", {**trip, "etaAt": (now() + timedelta(minutes=values["etaMinutes"])).isoformat(), "operationalStatus": "DELAYED"})
        incident = {**incident, "lastNote": values.get("note"), "updatedAt": iso()}
    elif operation == "RESOLVE":
        require(current in ["OPEN", "ACKNOWLEDGED", "RESOLVING"], "Incident cannot be resolved", 409)
        require(values.get("note", "").strip(), "A resolution note is required", 400)
        incident = {**incident, "status": "RESOLVED", "resolutionNote": values["note"].strip(), "resolvedAt": iso(), "resolvedById": party.id}
        active_ids = [ident for ident in trip.get("activeIncidentIds", []) if ident != incident["id"]]
        other_open = [row.data for row in rows(db, "incidents") if row.data["tripId"] == trip["id"] and row.id != incident["id"] and row.data["status"] != "RESOLVED"]
        operational_status = None
        if other_open:
            operational_status = "DELAYED" if all(item["kind"] in ["DELAY", "ROUTE_OBSTRUCTION"] for item in other_open) else "ACTION_REQUIRED"
        put(db, "trips", {**trip, "activeIncidentIds": active_ids, "operationalStatus": operational_status})
    elif operation == "RESUME_TRIP":
        require(current == "RESOLVED", "Resolve the incident before resuming the trip", 409)
        require(trip["status"] != "CANCELLED", "A cancelled trip cannot be resumed", 409)
        put(db, "trips", {**trip, "operationalStatus": None})
        put(db, "trucks", {**truck, "status": "ON_TRIP"})
    elif operation == "REOPEN_LOAD":
        require(current == "RESOLVED" and trip["status"] == "CANCELLED", "Only a resolved cancelled trip can be reopened", 409)
        prior_status = trip.get("events", [{}])[-2].get("status") if len(trip.get("events", [])) > 1 else "SCHEDULED"
        require(prior_status in ["SCHEDULED", "EN_ROUTE_PICKUP", "AT_PICKUP"], "Loads with collected cargo require manual recovery", 409)
        put(db, "loads", {**load, "status": "POSTED", "createdAt": iso()})
        put(db, "trucks", {**truck, "status": "AVAILABLE"})
        from app.services.domain import refresh_suggestions

        refresh_suggestions(db)

    incident = {
        **incident,
        "updates": [
            *incident.get("updates", []),
            {"action": operation, "at": iso(), "byUserId": party.id, "note": values.get("note")},
        ],
    }
    put(db, "incidents", incident)
    summary = values.get("note") or operation.replace("_", " ").title()
    audit(db, party, f"INCIDENT_{operation}", "TRIP", trip["id"], summary)
    recipients = {load["ownerId"], truck["transporterId"], truck.get("driverId")} - {None, party.id}
    for recipient in recipients:
        _notify(db, recipient, load["id"], f"Incident {operation.replace('_', ' ').lower()}", summary)
    return {"ok": True, "incident": incident}
