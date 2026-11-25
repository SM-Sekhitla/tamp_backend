from copy import deepcopy

from sqlalchemy import select, text

from app.api.deps import is_admin, party_payload
from app.core.errors import require
from app.core.security import iso, new_id
from app.db.models import OrganisationMembership, Party
from app.schemas.domain import SCHEMAS
from app.services.matching import (
    BODIES,
    distance,
    fleet_recommendations,
    matching_config,
    money,
    score,
)
from app.services.records import get, needed, put, rows

COLLECTIONS = [
    "loads",
    "vehicles",
    "vehicleDriverAssignments",
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
    "incidents",
    "invoices",
    "transactions",
    "dispatches",
]


def organisation_user_ids(db, party):
    active_org = db.info.get("active_organisation_id")
    if not active_org:
        return {party.id}
    return set(
        db.scalars(
            select(OrganisationMembership.user_id).where(
                OrganisationMembership.organisation_id == active_org,
                OrganisationMembership.status == "ACTIVE",
            )
        )
    ) or {party.id}


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


def attach_truck_document_metadata(db, truck, uploads_by_owner=None):
    if uploads_by_owner is None:
        upload_by_url = {
            row.data.get("url"): row.data
            for row in rows(db, "uploads")
            if row.owner_id == truck["transporterId"]
        }
    else:
        upload_by_url = uploads_by_owner.get(truck["transporterId"], {})
    categories = ["VEHICLE_LICENCE", "ROADWORTHY", "INSURANCE"]
    reviews = truck.get("documentReviews", {})
    truck["documentMetadata"] = [
        {
            "id": upload.get("id"),
            "name": upload.get("filename", f"Vehicle document {index + 1}"),
            "url": url,
            "contentType": upload.get("contentType"),
            "size": upload.get("size"),
            "category": categories[index] if index < len(categories) else "VEHICLE_DOCUMENT",
            "status": "PENDING",
            "reviewStatus": reviews.get(url, {}).get("reviewStatus", "PENDING"),
            "reviewReason": reviews.get(url, {}).get("reviewReason"),
            "reviewedAt": reviews.get(url, {}).get("reviewedAt"),
            "reviewedBy": reviews.get(url, {}).get("reviewedBy"),
            "uploadedBy": truck["transporterId"],
        }
        for index, url in enumerate(truck.get("documents", []))
        if (upload := upload_by_url.get(url)) is not None
    ]
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
    all_rows["vehicles"] = [v for v in all_rows["vehicles"] if v.get("category") != "PASSENGER"]
    if is_admin(party):
        uploads_by_owner = {}
        for upload in rows(db, "uploads"):
            uploads_by_owner.setdefault(upload.owner_id, {})[
                upload.data.get("url")
            ] = upload.data
        for truck in all_rows["trucks"]:
            attach_truck_document_metadata(db, truck, uploads_by_owner)
        return {**all_rows, "parties": [party_payload(p) for p in db.scalars(select(Party))]}
    pid = party.id
    organisation_users = organisation_user_ids(db, party)
    driver_scope = party.data["role"] == "DRIVER"
    if driver_scope:
        # A fleet driver sees only jobs explicitly offered or assigned to them.
        # Organisation membership must never expose the carrier's marketplace,
        # commercial matches, other vehicles, or other drivers' trips.
        driver_trip_ids = {
            d["tripId"]
            for d in all_rows["dispatches"]
            if d.get("driverId") == pid and d.get("status") in {"OFFERED", "ACCEPTED"}
        }
        driver_trip_ids.update(
            d["id"] for d in all_rows["trips"] if d.get("assignedDriverId") == pid
        )
        trips = {d["id"] for d in all_rows["trips"] if d["id"] in driver_trip_ids}
        trip_match_ids = {
            d["matchId"] for d in all_rows["trips"] if d["id"] in trips
        }
        matches = {d["id"] for d in all_rows["matches"] if d["id"] in trip_match_ids}
        loads = {d["loadId"] for d in all_rows["matches"] if d["id"] in matches}
        matched_trucks = {
            d["truckPostingId"] for d in all_rows["matches"] if d["id"] in matches
        }
        assigned_trucks = {
            d["id"] for d in all_rows["trucks"] if d.get("driverId") == pid
        }
        trucks = matched_trucks | assigned_trucks
        own_trucks = set(trucks)
        own_vehicle_ids = {
            d.get("vehicleId")
            for d in all_rows["trucks"]
            if d["id"] in own_trucks and d.get("vehicleId")
        }
    else:
        loads = {
            d["id"]
            for d in all_rows["loads"]
            if d["ownerId"] in organisation_users
            or (party.data["role"] == "TRANSPORTER" and d["status"] == "POSTED")
        }
        trucks = {
            d["id"]
            for d in all_rows["trucks"]
            if d["transporterId"] in organisation_users
            or d.get("driverId") == pid
            or (party.data["role"] == "FREIGHT_OWNER" and d["status"] == "AVAILABLE")
        }
        own_vehicle_ids = {
            d["id"] for d in all_rows["vehicles"] if d["ownerId"] in organisation_users
        }
        own_trucks = {
            d["id"]
            for d in all_rows["trucks"]
            if d["transporterId"] in organisation_users or d.get("driverId") == pid
        }
        matches = {
            d["id"]
            for d in all_rows["matches"]
            if any(
                item["id"] == d["loadId"] and item["ownerId"] in organisation_users
                for item in all_rows["loads"]
            )
            or d["truckPostingId"] in own_trucks
        }
        for m in all_rows["matches"]:
            if m["id"] in matches:
                loads.add(m["loadId"])
                trucks.add(m["truckPostingId"])
        trips = {d["id"] for d in all_rows["trips"] if d["matchId"] in matches}
    invoices = {d["id"] for d in all_rows["invoices"] if d["tripId"] in trips}
    result = {k: [] for k in COLLECTIONS}
    for k, ds in all_rows.items():
        for d in ds:
            visible = d["id"] in {
                "loads": loads,
                "trucks": trucks,
                "matches": matches,
                "trips": trips,
            }.get(k, set())
            if k == "vehicles":
                visible = d["id"] in own_vehicle_ids or is_admin(party)
            if k == "vehicleDriverAssignments":
                visible = d["vehicleId"] in own_vehicle_ids or d["driverId"] == pid
            if k in {"ratings", "disputes", "proofs", "incidents"}:
                visible = d["tripId"] in trips
            if k == "invoices":
                visible = d["id"] in invoices
            if k == "transactions":
                visible = d["invoiceId"] in invoices
            if k == "dispatches":
                visible = d["tripId"] in trips and (
                    party.data["role"] == "TRANSPORTER" or d["driverId"] == pid
                )
            if k == "acceptances":
                visible = d["matchId"] in matches
            if k == "reservations":
                visible = d["truckId"] in own_trucks or any(
                    item["id"] == d["loadId"] and item["ownerId"] in organisation_users
                    for item in all_rows["loads"]
                )
            if k in {"fuelLogs", "maintenance"}:
                visible = not driver_scope and d["truckId"] in own_trucks
            if k == "audit":
                visible = (
                    d["actorId"] == pid or d["subjectId"] in loads | own_trucks | matches | trips
                )
            if visible:
                result[k].append(d)
    related = {pid} if driver_scope else set(organisation_users)
    contacts = {pid} if driver_scope else set(organisation_users)
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
            t.pop("documentMetadata", None)
            if t["transporterId"] not in contacts:
                t["registration"] = "•••" + t["registration"][-3:]
        else:
            attach_truck_document_metadata(db, t)
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


def sanitize_load(db, party, data, old=None, *, asset_move=False):
    organisation_users = organisation_user_ids(db, party)
    linked_move = bool(old and old.get("assetMoveId"))
    require(
        data["ownerId"] in organisation_users
        and (party.data["role"] == "FREIGHT_OWNER" or (
            party.data["role"] == "TRANSPORTER" and (asset_move or linked_move)
        ))
    )
    if not asset_move:
        for key in ("assetMoveId", "transportDimensions"):
            data.pop(key, None)
            if linked_move:
                data[key] = old[key]
        if linked_move:
            data["weightKg"] = old["weightKg"]
            data["cargoType"] = old["cargoType"]
    if old:
        require(old["ownerId"] in organisation_users)
        require(
            old["status"] in ["POSTED", "DRAFT", "EXPIRED", "CANCELLED"]
            and data["status"] in ["POSTED", "DRAFT", "CANCELLED"],
            "Use match commands to change committed loads",
            409,
        )
    else:
        require(data["status"] in ["POSTED", "DRAFT"], "Invalid initial load status", 400)
    for key, collection in (("customerId", "freightCustomers"), ("templateId", "loadTemplates")):
        ident = data.get(key)
        if ident:
            linked = get(db, collection, ident)
            require(
                linked is not None
                and linked.organisation_id == db.info.get("active_organisation_id"),
                f"Invalid {key}",
                400,
            )
    if not data.get("stops"):
        data["stops"] = [
            {"id": new_id("LS"), "sequence": 0, "kind": "PICKUP", "place": data["origin"], "window": data["pickupWindow"], "cargoWeightKg": data["weightKg"], "status": "PENDING"},
            {"id": new_id("LS"), "sequence": 1, "kind": "DROPOFF", "place": data["destination"], "window": {"from": data["deliveryBy"], "to": data["deliveryBy"]}, "cargoWeightKg": data["weightKg"], "status": "PENDING"},
        ]
    data["origin"] = data["stops"][0]["place"]
    data["destination"] = data["stops"][-1]["place"]
    data["pickupWindow"] = data["stops"][0]["window"]
    data["deliveryBy"] = data["stops"][-1]["window"]["to"]
    data["distanceKm"] = round(sum(distance(a["place"], b["place"]) for a, b in zip(data["stops"], data["stops"][1:])), 1)
    data["requiredBodyTypes"] = BODIES[data["cargoType"]]
    platform_quote = money(data)
    data["platformQuote"] = platform_quote
    data["targetRate"] = (
        data["offeredRate"] if data.get("pricingMode") == "CUSTOM" else platform_quote
    )
    data["createdAt"] = iso() if not old or old["status"] == "EXPIRED" else old["createdAt"]
    data["reference"] = data["id"]
    return data


def sync(db, party, delta):
    lock_domain(db)
    changes = delta.upserts
    organisation_users = organisation_user_ids(db, party)
    # Server-owned records cannot be authored through the legacy delta API.
    for k in ["trips", "acceptances", "proofs", "incidents", "invoices", "transactions", "dispatches"]:
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
                if old and data["ownerId"] not in organisation_users:
                    # A carrier request may include an unchanged public load.
                    require(old == data, "Cannot modify another owner's load")
                    continue
                if old and old["status"] == "MATCHED" and data["status"] == "POSTED":
                    # Decline is processed through its match row below.
                    continue
                data = sanitize_load(db, party, data, old)
            elif k == "trucks":
                from app.services.rbac import require_permission

                require_permission(db, party, "trucks.manage")
                transporter = db.get(Party, data["transporterId"])
                owner_operator = bool(
                    transporter
                    and (
                        transporter.data.get("ownerOperator")
                        or (transporter.onboarding or {}).get("driver", {}).get("workType")
                        == "OWNER"
                    )
                )
                if owner_operator:
                    existing_owner_trucks = [
                        item.data
                        for item in rows(db, "trucks")
                        if item.data["transporterId"] == transporter.id
                        and (old is None or item.data["id"] != old["id"])
                    ]
                    require(
                        old is not None or not existing_owner_trucks,
                        "Owner-operators can register one truck only",
                        409,
                    )
                    data["driverId"] = transporter.id
                require(
                    bool(data.get("licenceNumber", "").strip())
                    and bool(data.get("licenceExpiry")),
                    "Vehicle licence number and expiry date are required",
                    400,
                )
                require(
                    party.data["role"] in ["TRANSPORTER", "ADMIN"]
                    and (data["transporterId"] in organisation_users or is_admin(party))
                )
                require(not old or old["transporterId"] == data["transporterId"])
                # Review decisions are platform-owned metadata. Accept them in
                # the round-trip schema, but never trust a client-supplied edit.
                if old:
                    data["documentReviews"] = deepcopy(old.get("documentReviews", {}))
                else:
                    data.pop("documentReviews", None)
                # This view model is rebuilt from Upload records by snapshot().
                data.pop("documentMetadata", None)
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
                    owner_driver = (
                        driver is not None
                        and driver.id == data["transporterId"]
                        and driver.data["role"] == "TRANSPORTER"
                    )
                    require(
                        driver is not None
                        and (driver.data["role"] == "DRIVER" or owner_driver)
                        and driver.data["verification"] == "VERIFIED",
                        "Choose a verified driver",
                        400,
                    )
                    organisation_id = (
                        row.organisation_id
                        if row is not None
                        else db.info.get("active_organisation_id")
                    )
                    driver_membership = db.scalar(
                        select(OrganisationMembership).where(
                            OrganisationMembership.organisation_id == organisation_id,
                            OrganisationMembership.user_id == driver.id,
                            OrganisationMembership.status == "ACTIVE",
                        )
                    )
                    require(
                        owner_driver or driver_membership is not None,
                        "Choose a driver from this truck organisation",
                        400,
                    )
                from app.services.storage import own_upload

                for url in data.get("photos", []):
                    own_upload(db, party, url, "TRUCK_PHOTO")
                for url in data.get("documents", []):
                    own_upload(db, party, url, "TRUCK_DOCUMENT")
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
                        matching_config(db),
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
                    if party.id == load["ownerId"]:
                        driver_id = trip.get("assignedDriverId") or truck.get("driverId")
                        if driver_id:
                            data["driverId"] = driver_id
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
            if k == "trucks":
                from app.services.fleet import sync_vehicle_for_truck

                sync_vehicle_for_truck(db, party, data, old)
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
                # Freight-owner ride feedback also contributes to the assigned
                # driver's personal reputation while retaining the carrier's
                # company-level rating above.
                driver_id = data.get("driverId")
                if driver_id:
                    driver = db.get(Party, driver_id)
                    if driver is not None:
                        driver_ratings = [
                            r.data["stars"]
                            for r in rows(db, "ratings")
                            if r.data.get("driverId") == driver_id
                        ]
                        driver.data = {
                            **driver.data,
                            "ratingCount": len(driver_ratings),
                            "ratingAvg": round(
                                sum(driver_ratings) / len(driver_ratings), 1
                            )
                            if len(driver_ratings) >= 3
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
    from app.services.engagements import expire_stale_engagements
    from app.services.notifications import notify_organisation, notify_user
    from app.services.rbac import ROLE_IDS

    expire_stale_engagements(db)

    parties = {p.id: p.data for p in db.scalars(select(Party))}
    from app.services.fleet import with_vehicle_verification

    # Vehicle verification and matching configuration are invariant for this
    # refresh. Resolve each once instead of inside the load×truck loop.
    trucks = [with_vehicle_verification(db, r.data) for r in rows(db, "trucks")]
    loads = [r.data for r in rows(db, "loads")]
    config = matching_config(db)
    existing = {
        (r.data["loadId"], r.data["truckPostingId"]): r
        for r in rows(db, "matches")
        if r.data["status"] not in ["WITHDRAWN", "EXPIRED"]
    }
    excluded_pairs = {
        (r.data["loadId"], r.data["truckPostingId"])
        for r in rows(db, "matches")
        if r.data.get("excludedFromRematch")
    }
    owner_match_notifications = {
        (row.organisation_id, row.owner_id, row.data.get("subjectId"))
        for row in rows(db, "notifications")
        if row.data.get("title") == "New truck match"
    }
    for load in loads:
        selected_count = sum(
            row.data["loadId"] == load["id"]
            and row.data["status"] in ["ACCEPTED", "CONFIRMED"]
            for row in rows(db, "matches")
        )
        has_open_fleet_slot = (
            load["status"] == "MATCHED"
            and selected_count < max(1, int(load.get("trucksRequired") or 1))
        )
        if (
            load["status"] != "POSTED"
            and not has_open_fleet_slot
        ) or dt(load["pickupWindow"]["to"]) < now():
            continue
        scored_inventory = [
            score(
                load,
                truck,
                parties[truck["transporterId"]],
                parties[load["ownerId"]],
                config,
            )
            for truck in trucks
        ]
        scored_by_truck = {
            result["truck"]["id"]: result for result in scored_inventory
        }
        fleet = fleet_recommendations(
            scored_inventory,
            load.get("trucksRequired", 1),
            config["minimumScore"],
        )
        for truck in trucks:
            if (load["id"], truck["id"]) in excluded_pairs:
                continue
            result = scored_by_truck[truck["id"]]
            old = existing.get((load["id"], truck["id"]))
            if old and old.data["status"] != "SUGGESTED":
                continue
            if not result["passed"] or result["score"] < config["minimumScore"]:
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
                **fleet["metadata"].get(truck["id"], {}),
            )
            put(db, "matches", data, load["ownerId"])
            load_row = get(db, "loads", load["id"])
            owner_notification_key = (
                load_row.organisation_id,
                load["ownerId"],
                data["id"],
            )
            if owner_notification_key not in owner_match_notifications:
                owner_detail = (
                    f"{truck['bodyType'].replace('_', ' ').title()} "
                    f"{truck['registration']} from "
                    f"{parties[truck['transporterId']]['companyName']} matches "
                    f"{load['origin']['label']} to {load['destination']['label']} "
                    f"at {result['score']:.0f}%."
                )
                if load_row.organisation_id:
                    notify_organisation(
                        db,
                        load_row.organisation_id,
                        title="New truck match",
                        detail=owner_detail,
                        subject_type="MATCH",
                        subject_id=data["id"],
                        role_ids={ROLE_IDS["FREIGHT_OWNER"], ROLE_IDS["CARGO_OPERATOR"]},
                    )
                else:
                    notify_user(
                        db,
                        load["ownerId"],
                        title="New truck match",
                        detail=owner_detail,
                        subject_type="MATCH",
                        subject_id=data["id"],
                    )
                owner_match_notifications.add(owner_notification_key)
            if old is None:
                truck_row = get(db, "trucks", truck["id"])
                notify_organisation(
                    db,
                    truck_row.organisation_id,
                    title="New matching load",
                    detail=(
                        f"{load['origin']['label']} to {load['destination']['label']} "
                        f"matches {truck['registration']} at {result['score']:.0f}%"
                    ),
                    subject_type="LOAD",
                    subject_id=load["id"],
                    role_ids={ROLE_IDS["TRANSPORTER"], ROLE_IDS["DISPATCHER"]},
                )
