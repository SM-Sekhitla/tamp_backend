import hashlib
import json

from app.core.client_ip import client_ip
from app.core.errors import require
from app.core.security import iso, new_id, now
from app.db.models import Party
from app.services.domain import audit, lock_domain, truck_access
from app.services.matching import dt, score
from app.services.records import needed, put, rows

STEPS = ["SCHEDULED", "AT_PICKUP", "LOADED", "IN_TRANSIT", "AT_DROPOFF", "DELIVERED", "COMPLETED"]


def receipt(db, party, match, request):
    body = dict(
        id=new_id("AC"),
        matchId=match["id"],
        party=party.data["role"],
        byUserId=party.id,
        at=iso(),
        ip=client_ip(request),
    )
    body["hash"] = hashlib.sha256(
        json.dumps({**body, "terms": match}, sort_keys=True).encode()
    ).hexdigest()
    put(db, "acceptances", body, party.id)


def notify(db, party_id, subject, title):
    put(
        db,
        "notifications",
        dict(
            id=new_id("NT"),
            title=title,
            detail=subject,
            at=iso(),
            unread=True,
            tone="info",
            subjectType="LOAD",
            subjectId=subject,
        ),
        party_id,
    )


def handle(action, data, db, request, response, party):
    d = data.model_dump(mode="json", exclude_none=True)
    lock_domain(db)
    load = needed(db, "loads", d["loadId"], True).data
    candidates = [
        r.data
        for r in rows(db, "matches")
        if r.data["loadId"] == load["id"]
        and r.data["status"] in ["SUGGESTED", "OFFERED", "ACCEPTED", "CONFIRMED"]
        and (not d.get("truckId") or r.data["truckPostingId"] == d["truckId"])
    ]
    candidates.sort(
        key=lambda m: {"CONFIRMED": 0, "ACCEPTED": 1, "OFFERED": 2, "SUGGESTED": 3}[m["status"]]
    )
    require(bool(candidates), "No actionable match found", 404)
    match = candidates[0]
    truck = needed(db, "trucks", match["truckPostingId"], True).data
    if action == "advanceTrip":
        require(
            truck_access(party, truck) and party.data["role"] in ["DRIVER", "TRANSPORTER"],
            "Only the assigned driver or carrier can advance trips",
        )
        trip = next((r.data for r in rows(db, "trips") if r.data["matchId"] == match["id"]), None)
        require(trip is not None and trip["status"] in STEPS[:-1], "Trip cannot advance", 409)
        status = STEPS[STEPS.index(trip["status"]) + 1]
        if status == "DELIVERED":
            proof = d.get("proof")
            require(
                proof is not None and proof.get("recipientName", "").strip(),
                "Proof of delivery requires a recipient",
                400,
            )
            if proof.get("photoUrl"):
                from app.services.storage import own_upload

                own_upload(db, party, proof["photoUrl"], "POD")
            put(
                db,
                "proofs",
                dict(
                    **proof, id=new_id("PF"), tripId=trip["id"], loadId=load["id"], capturedAt=iso()
                ),
                party.id,
            )
        progress = round(STEPS.index(status) / (len(STEPS) - 1) * 100)
        trip = {
            **trip,
            "status": status,
            "progressPct": progress,
            "events": [*trip["events"], {"status": status, "at": iso(), "byUserId": party.id}],
        }
        put(db, "trips", trip)
        put(db, "trucks", {**truck, "status": "AVAILABLE" if status == "COMPLETED" else "ON_TRIP"})
        if status == "COMPLETED":
            put(db, "loads", {**load, "status": "COMPLETED"})
        audit(db, party, "TRIP_STATUS_CHANGED", "TRIP", trip["id"], status)
        notify(db, load["ownerId"], load["id"], f"Trip {status.lower().replace('_', ' ')}")
        return {"ok": True, "status": status, "progressPct": progress}
    require(party.data["verification"] == "VERIFIED", "Account verification is required", 403)
    if action == "confirmMatch":
        require(party.id == truck["transporterId"], "Only this carrier may confirm")
        require(
            match["status"] == "ACCEPTED" and load["status"] == "MATCHED",
            "Owner acceptance required",
            409,
        )
    else:
        require(party.id == load["ownerId"], "Only the load owner may accept")
        require(load["status"] == "POSTED", "Load is no longer available", 409)
        require(
            match["status"]
            in (["OFFERED"] if action == "acceptRequest" else ["SUGGESTED", "OFFERED"]),
            "Match cannot be accepted",
            409,
        )
    require(dt(match["expiresAt"]) > now(), "Match expired", 409)
    operator = db.get(Party, truck["transporterId"])
    owner = db.get(Party, load["ownerId"])
    checked = score(
        load,
        {
            **truck,
            "status": "AVAILABLE"
            if action == "confirmMatch" and truck["status"] == "RESERVED"
            else truck["status"],
        },
        operator.data,
        owner.data,
    )
    require(checked["passed"], "Truck no longer meets the load requirements", 409)
    other_active = [
        m.data
        for m in rows(db, "matches")
        if m.data["id"] != match["id"]
        and m.data["truckPostingId"] == truck["id"]
        and m.data["status"] in ["ACCEPTED", "CONFIRMED"]
        and needed(db, "loads", m.data["loadId"]).data["status"]
        not in ["COMPLETED", "CLOSED", "CANCELLED"]
    ]
    require(not other_active, "Truck already committed", 409)
    final = action in ["confirmMatch", "acceptRequest"]
    match = {
        **match,
        "status": "CONFIRMED" if final else "ACCEPTED",
        "agreedRate": load["targetRate"],
    }
    match["confirmedByTransporterAt" if action == "confirmMatch" else "confirmedByOwnerAt"] = iso()
    put(db, "matches", match)
    receipt(db, party, match, request)
    put(db, "loads", {**load, "status": "CONFIRMED" if final else "MATCHED"})
    put(db, "trucks", {**truck, "status": "RESERVED"})
    for r in rows(db, "matches"):
        if (
            r.id != match["id"]
            and r.data["loadId"] == load["id"]
            and r.data["status"] in ["SUGGESTED", "OFFERED"]
        ):
            r.data = {**r.data, "status": "WITHDRAWN"}
    audit(
        db,
        party,
        "ENGAGEMENT_CONFIRMED" if final else "MATCH_ACCEPTED",
        "LOAD",
        load["id"],
        match["status"],
    )
    notify(
        db,
        truck["transporterId"] if party.id == load["ownerId"] else load["ownerId"],
        load["id"],
        match["status"].title(),
    )
    if final:
        trip_id = new_id("TP")
        put(
            db,
            "trips",
            dict(
                id=trip_id,
                matchId=match["id"],
                status="SCHEDULED",
                events=[dict(status="SCHEDULED", at=iso(), byUserId=party.id)],
                simulatedPosition=None,
                progressPct=0,
                etaAt=None,
            ),
            load["ownerId"],
        )
        return {"ok": True, "tripId": trip_id}
    return {"ok": True}
