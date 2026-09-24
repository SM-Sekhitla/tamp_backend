from copy import deepcopy

from sqlalchemy import select, text

from app.api.deps import is_admin, party_payload
from app.core.errors import require
from app.core.security import iso, new_id
from app.db.models import Party
from app.schemas.domain import SCHEMAS
from app.services.matching import BODIES, distance, money, score
from app.services.records import get, needed, put, rows

COLLECTIONS = [
    "loads",
    "trucks",
    "matches",
    "trips",
    "ratings",
    "disputes",
    "acceptances",
    "reservations",
    "audit",
    "fuelLogs",
    "maintenance",
    "proofs",
]


def lock_domain(db):
    # Serialize commercial transitions and legacy snapshot mutations across workers.
    db.execute(text("SELECT pg_advisory_xact_lock(847219)"))


def audit(db, party, event, kind, subject, message):
    put(
        db,
        "audit",
        dict(
            id=new_id("AU"),
            eventId=event,
            eventType=event,
            actorId=party.id,
            actorRole=party.data["role"],
            subjectType=kind,
            subjectId=subject,
            summary=message,
            at=iso(),
        ),
        party.id,
    )
    for endpoint in rows(db, "webhooks"):
        if endpoint.data["active"] and (
            event in endpoint.data["events"] or "*" in endpoint.data["events"]
        ):
            ident = new_id("WD")
            put(
                db,
                "deliveries",
                dict(
                    id=ident,
                    endpointId=endpoint.id,
                    event=event,
                    status="PENDING",
                    attempts=0,
                    responseCode=None,
                    lastError=None,
                    createdAt=iso(),
                    nextAttempt=iso(),
                    payload={
                        "id": ident,
                        "event": event,
                        "subjectType": kind,
                        "subjectId": subject,
                        "at": iso(),
                    },
                ),
                endpoint.owner_id,
            )


def snapshot(db, party):
    all_rows = {k: [deepcopy(r.data) for r in rows(db, k)] for k in COLLECTIONS}
    if is_admin(party):
        return {**all_rows, "parties": [party_payload(p) for p in db.scalars(select(Party))]}
    pid = party.id
    loads = {
        d["id"]
        for d in all_rows["loads"]
        if d["ownerId"] == pid or (party.data["role"] == "TRANSPORTER" and d["status"] == "POSTED")
    }
    trucks = {
        d["id"]
        for d in all_rows["trucks"]
        if d["transporterId"] == pid
        or d.get("driverId") == pid
        or (party.data["role"] == "FREIGHT_OWNER" and d["status"] == "AVAILABLE")
    }
    own_trucks = {
        d["id"] for d in all_rows["trucks"] if d["transporterId"] == pid or d.get("driverId") == pid
    }
    matches = {
        d["id"]
        for d in all_rows["matches"]
        if any(item["id"] == d["loadId"] and item["ownerId"] == pid for item in all_rows["loads"])
        or d["truckPostingId"] in own_trucks
    }
    for m in all_rows["matches"]:
        if m["id"] in matches:
            loads.add(m["loadId"])
            trucks.add(m["truckPostingId"])
    trips = {d["id"] for d in all_rows["trips"] if d["matchId"] in matches}
    result = {k: [] for k in COLLECTIONS}
    for k, ds in all_rows.items():
        for d in ds:
            visible = d["id"] in {
                "loads": loads,
                "trucks": trucks,
                "matches": matches,
                "trips": trips,
            }.get(k, set())
            if k in {"ratings", "disputes", "proofs"}:
                visible = d["tripId"] in trips
            if k == "acceptances":
                visible = d["matchId"] in matches
            if k == "reservations":
                visible = d["truckId"] in own_trucks or any(
                    item["id"] == d["loadId"] and item["ownerId"] == pid
                    for item in all_rows["loads"]
                )
            if k in {"fuelLogs", "maintenance"}:
                visible = d["truckId"] in own_trucks
            if k == "audit":
                visible = (
                    d["actorId"] == pid or d["subjectId"] in loads | own_trucks | matches | trips
                )
            if visible:
                result[k].append(d)
    related = {pid}
    contacts = {pid}
    for t in result["trucks"]:
        related.add(t["transporterId"])
        if t.get("driverId"):
            related.add(t["driverId"])
    for item in result["loads"]:
        related.add(item["ownerId"])
    for m in result["matches"]:
        if m["status"] == "CONFIRMED":
            item = next(item for item in result["loads"] if item["id"] == m["loadId"])
            t = next(t for t in result["trucks"] if t["id"] == m["truckPostingId"])
            contacts.update([item["ownerId"], t["transporterId"], t.get("driverId")])
    for t in result["trucks"]:
        if t["id"] in own_trucks:
            contacts.update([t["transporterId"], t.get("driverId")])
    parties = []
    for p in db.scalars(select(Party).where(Party.id.in_(related))):
        d = deepcopy(p.data)
        if p.id not in contacts:
            d.update(email="", phone="")
        if p.id != pid:
            d.pop("kycDocument", None)
        parties.append(d)
    for t in result["trucks"]:
        if t["id"] not in own_trucks:
            t.pop("documents", None)
            if t["transporterId"] not in contacts:
                t["registration"] = "•••" + t["registration"][-3:]
    result["parties"] = parties
    return result


def truck_access(party, truck):
    return party.id in [truck["transporterId"], truck.get("driverId")] or is_admin(party)


def trip_context(db, trip_id, party):
    trip = needed(db, "trips", trip_id).data
    match = needed(db, "matches", trip["matchId"]).data
    load = needed(db, "loads", match["loadId"]).data
    truck = needed(db, "trucks", match["truckPostingId"]).data
    require(load["ownerId"] == party.id or truck_access(party, truck))
    return trip, load, truck


def sanitize_load(db, party, data, old=None):
    require(party.data["role"] == "FREIGHT_OWNER" and data["ownerId"] == party.id)
    if old:
        require(old["ownerId"] == party.id)
        require(
            old["status"] in ["POSTED", "DRAFT", "EXPIRED", "CANCELLED"]
            and data["status"] in ["POSTED", "DRAFT", "CANCELLED"],
            "Use match commands to change committed loads",
            409,
        )
    else:
        require(data["status"] in ["POSTED", "DRAFT"], "Invalid initial load status", 400)
    data["distanceKm"] = distance(data["origin"], data["destination"])
    data["requiredBodyTypes"] = BODIES[data["cargoType"]]
    data["targetRate"] = money(data)
    data["createdAt"] = iso() if not old or old["status"] == "EXPIRED" else old["createdAt"]
    data["reference"] = data["id"]
    return data


def sync(db, party, delta):
    lock_domain(db)
    changes = delta.upserts
    # Server-owned records cannot be authored through the legacy delta API.
    for k in ["trips", "acceptances"]:
        for d in changes.get(k, []):
            old = needed(db, k, d.get("id")).data
            allowed = snapshot(db, party)[k]
            require(any(r["id"] == old["id"] for r in allowed))
            # Existing clients mirror the dispute flag; derive it from disputes instead.
            require(
                {a: b for a, b in d.items() if a != "disputed"}
                == {a: b for a, b in old.items() if a != "disputed"},
                "Use authoritative trip commands",
                409,
            )
    for k in [
        "loads",
        "trucks",
        "matches",
        "reservations",
        "ratings",
        "disputes",
        "fuelLogs",
        "maintenance",
        "proofs",
    ]:
        for raw in changes.get(k, []):
            data = (
                SCHEMAS[k]
                .model_validate(raw)
                .model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            row = get(db, k, data["id"], True)
            old = row.data if row else None
            if (
                old
                and SCHEMAS[k]
                .model_validate(old)
                .model_dump(mode="json", by_alias=True, exclude_none=True)
                == data
            ):
                continue
            owner = party.id
            if k == "loads":
                if old and data["ownerId"] != party.id:
                    # A carrier request may include an unchanged public load.
                    require(old == data, "Cannot modify another owner's load")
                    continue
                if old and old["status"] == "MATCHED" and data["status"] == "POSTED":
                    # Decline is processed through its match row below.
                    continue
                data = sanitize_load(db, party, data, old)
            elif k == "trucks":
                require(
                    party.data["role"] in ["TRANSPORTER", "ADMIN"]
                    and (data["transporterId"] == party.id or is_admin(party))
                )
                require(not old or old["transporterId"] == data["transporterId"])
                require(
                    (old and data["status"] == old["status"])
                    or data["status"] in ["AVAILABLE", "OFFLINE"],
                    "Truck commitment is server managed",
                    409,
                )
                if old and old["status"] in ["RESERVED", "ON_TRIP"]:
                    require(
                        all(
                            data.get(key) == old.get(key)
                            for key in [
                                "transporterId",
                                "driverId",
                                "registration",
                                "bodyType",
                                "payloadCapacityKg",
                                "currentLocation",
                                "status",
                            ]
                        ),
                        "Cannot edit an engaged truck",
                        409,
                    )
                if data.get("driverId"):
                    driver = db.get(Party, data["driverId"])
                    require(
                        driver is not None
                        and driver.data["role"] == "DRIVER"
                        and driver.data["verification"] == "VERIFIED",
                        "Choose a verified driver",
                        400,
                    )
                from app.services.storage import own_upload

                for url in data.get("photos", []):
                    own_upload(db, party, url, "TRUCK_PHOTO")
                owner = data["transporterId"]
            elif k == "matches":
                load = needed(db, "loads", data["loadId"]).data
                truck = needed(db, "trucks", data["truckPostingId"]).data
                require(load["ownerId"] == party.id or truck["transporterId"] == party.id)
                if (
                    not old
                    and data["status"] == "SUGGESTED"
                    and any(
                        m.data["loadId"] == data["loadId"]
                        and m.data["truckPostingId"] == data["truckPostingId"]
                        and m.data["status"] == "SUGGESTED"
                        for m in rows(db, "matches")
                    )
                ):
                    continue
                if old:
                    require(
                        old["loadId"] == data["loadId"]
                        and old["truckPostingId"] == data["truckPostingId"]
                    )
                    require(
                        data["status"] == "REJECTED"
                        and old["status"] in ["SUGGESTED", "OFFERED", "ACCEPTED"],
                        "Use match commands",
                        409,
                    )
                    if old["status"] == "ACCEPTED":
                        require(truck["transporterId"] == party.id)
                    data = {
                        **old,
                        "status": "REJECTED",
                        "rejectionReason": data.get("rejectionReason", ""),
                    }
                    if old["status"] == "ACCEPTED":
                        put(db, "loads", {**load, "status": "POSTED"})
                        put(db, "trucks", {**truck, "status": "AVAILABLE"})
                else:
                    require(
                        data["status"] in ["SUGGESTED", "OFFERED"] and load["status"] == "POSTED"
                    )
                    if data["status"] == "OFFERED":
                        require(truck["transporterId"] == party.id)
                    result = score(
                        load,
                        truck,
                        db.get(Party, truck["transporterId"]).data,
                        db.get(Party, load["ownerId"]).data,
                    )
                    require(result["passed"], "Truck does not meet load requirements", 409)
                    data.update(
                        score=result["score"],
                        breakdown=result["breakdown"],
                        hardFilterResults=result["hardFilterResults"],
                        initiatedBy="TRANSPORTER" if data["status"] == "OFFERED" else "SYSTEM",
                    )
                    data.pop("agreedRate", None)
                    data.pop("confirmedByOwnerAt", None)
                    data.pop("confirmedByTransporterAt", None)
                owner = load["ownerId"]
            elif k in ["fuelLogs", "maintenance", "reservations"]:
                truck = needed(db, "trucks", data["truckId"]).data
                require(truck_access(party, truck))
                require(not old or old["truckId"] == data["truckId"])
                data["transporterId"] = truck["transporterId"]
                if k != "reservations" and truck.get("driverId"):
                    data["driverId"] = truck["driverId"]
                owner = truck["transporterId"]
                if k == "reservations":
                    require(truck["transporterId"] == party.id)
                    load = needed(db, "loads", data["loadId"]).data
                    require(data["status"] in (["RESERVED", "RELEASED"] if old else ["RESERVED"]))
                    require(
                        load["status"] == "POSTED" or (old and data["status"] == "RELEASED"),
                        "Load no longer open",
                        409,
                    )
                    require(not old or old["loadId"] == data["loadId"])
            else:
                trip, load, truck = trip_context(db, data["tripId"], party)
                require(not old or old.get("tripId") == data["tripId"])
                if k == "ratings":
                    require(not old, "Ratings cannot be rewritten", 409)
                    require(
                        trip["status"] in ["DELIVERED", "COMPLETED"],
                        "Rate completed deliveries",
                        409,
                    )
                    require(party.id in [load["ownerId"], truck["transporterId"]])
                    data.update(
                        raterId=party.id,
                        rateeId=truck["transporterId"]
                        if party.id == load["ownerId"]
                        else load["ownerId"],
                        createdAt=iso(),
                    )
                elif k == "disputes":
                    if old:
                        require(is_admin(party))
                        data = {
                            **old,
                            **{
                                key: data[key]
                                for key in ["status", "resolutionNote"]
                                if key in data
                            },
                        }
                        if data["status"] in ["RESOLVED", "DISMISSED"]:
                            data["resolvedAt"] = iso()
                    else:
                        data.update(raisedById=party.id, status="OPEN", createdAt=iso())
                        data.pop("resolvedAt", None)
                    put(
                        db,
                        "trips",
                        {**trip, "disputed": data["status"] in ["OPEN", "UNDER_REVIEW"]},
                    )
                elif k == "proofs":
                    require(truck_access(party, truck))
                    require(
                        data["loadId"] == load["id"]
                        and trip["status"] in ["AT_DROPOFF", "DELIVERED"]
                    )
                    if data.get("photoUrl"):
                        from app.services.storage import own_upload

                        own_upload(db, party, data["photoUrl"], "POD")
            put(db, k, data, owner)
            audit(
                db,
                party,
                "RECORD_UPDATED",
                {"loads": "LOAD", "trucks": "TRUCK", "matches": "MATCH", "disputes": "DISPUTE"}.get(
                    k, "TRIP"
                ),
                data["id"],
                f"{k} saved",
            )
            if k == "ratings":
                target = db.get(Party, data["rateeId"])
                ratings = [
                    r.data["stars"] for r in rows(db, "ratings") if r.data["rateeId"] == target.id
                ]
                target.data = {
                    **target.data,
                    "ratingCount": len(ratings),
                    "ratingAvg": round(sum(ratings) / len(ratings), 1)
                    if len(ratings) >= 3
                    else None,
                }
    for k, ids in delta.deleteIds.items():
        if k == "audit":
            continue  # UI truncates its local audit view; history remains immutable.
        for ident in ids:
            row = get(db, k, ident, True)
            if not row:
                continue
            require(k == "matches", "Records cannot be deleted through snapshots")
            truck = needed(db, "trucks", row.data["truckPostingId"]).data
            load = needed(db, "loads", row.data["loadId"]).data
            require(
                (row.data["status"] == "OFFERED" and truck["transporterId"] == party.id)
                or (row.data["status"] == "SUGGESTED" and load["ownerId"] == party.id)
            )
            row.data = {**row.data, "status": "WITHDRAWN"}
    refresh_suggestions(db)
    return {"ok": True, "snapshot": snapshot(db, party)}


def refresh_suggestions(db):
    """Recompute discoverable suggestions after inventory/verification changes."""
    from datetime import timedelta

    from app.core.security import now
    from app.services.matching import dt

    parties = {p.id: p.data for p in db.scalars(select(Party))}
    trucks = [r.data for r in rows(db, "trucks")]
    loads = [r.data for r in rows(db, "loads")]
    existing = {
        (r.data["loadId"], r.data["truckPostingId"]): r
        for r in rows(db, "matches")
        if r.data["status"] not in ["WITHDRAWN", "EXPIRED"]
    }
    for load in loads:
        if load["status"] != "POSTED" or dt(load["pickupWindow"]["to"]) < now():
            continue
        for truck in trucks:
            result = score(load, truck, parties[truck["transporterId"]], parties[load["ownerId"]])
            old = existing.get((load["id"], truck["id"]))
            if old and old.data["status"] != "SUGGESTED":
                continue
            if not result["passed"]:
                if old:
                    old.data = {**old.data, "status": "EXPIRED"}
                continue
            data = dict(
                id=old.id if old else new_id("MT"),
                loadId=load["id"],
                truckPostingId=truck["id"],
                score=result["score"],
                breakdown=result["breakdown"],
                hardFilterResults=result["hardFilterResults"],
                status="SUGGESTED",
                initiatedBy="SYSTEM",
                expiresAt=min(
                    now() + timedelta(days=1), dt(load["pickupWindow"]["to"])
                ).isoformat(),
                createdAt=old.data["createdAt"] if old else iso(),
            )
            put(db, "matches", data, load["ownerId"])
