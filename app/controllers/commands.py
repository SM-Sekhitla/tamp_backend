import hashlib
import hmac
import json
from datetime import timedelta

from sqlalchemy import select

from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.errors import AppError, require
from app.core.security import iso, new_id, now
from app.db.models import OrganisationMembership, Party, VehiclePosition
from app.services.domain import audit, lock_domain, organisation_user_ids
from app.services.engagements import expire_stale_engagements
from app.services.lifecycle import require_transition
from app.services.maps import verify_route_token
from app.services.matching import dt, matching_config, score
from app.services.records import needed, put, rows

STEPS = [
    "SCHEDULED",
    "EN_ROUTE_PICKUP",
    "AT_PICKUP",
    "LOADED",
    "IN_TRANSIT",
    "AT_DROPOFF",
    "DELIVERED",
    "COMPLETED",
]


def load_stops(load):
    return load.get("stops") or [
        {"id": f"{load['id']}-pickup", "sequence": 0, "kind": "PICKUP", "place": load["origin"], "window": load["pickupWindow"], "status": "PENDING"},
        {"id": f"{load['id']}-dropoff", "sequence": 1, "kind": "DROPOFF", "place": load["destination"], "window": {"from": load["deliveryBy"], "to": load["deliveryBy"]}, "status": "PENDING"},
    ]


def next_trip_step(trip, stops):
    status = trip["status"]
    index = min(trip.get("currentStopIndex", 0), len(stops) - 1)
    if status == "SCHEDULED":
        return "EN_ROUTE_PICKUP", index
    if status == "EN_ROUTE_PICKUP":
        return "AT_PICKUP", index
    if status == "AT_PICKUP":
        return "LOADED", index
    if status in ["LOADED", "DELIVERED"]:
        if index + 1 >= len(stops):
            return "COMPLETED", index
        index += 1
        return ("EN_ROUTE_PICKUP" if stops[index]["kind"] == "PICKUP" else "IN_TRANSIT"), index
    if status == "IN_TRANSIT":
        return "AT_DROPOFF", index
    if status == "AT_DROPOFF":
        return "DELIVERED", index
    raise ValueError("Trip cannot advance")


def stable_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def business_terms(load, truck, match):
    return {
        "matchId": match["id"],
        "loadId": load["id"],
        "truckId": truck["id"],
        "cargoType": load["cargoType"],
        "weightKg": load["weightKg"],
        "volumeM3": load.get("volumeM3"),
        "origin": load["origin"],
        "destination": load["destination"],
        "pickupWindow": load["pickupWindow"],
        "deliveryBy": load["deliveryBy"],
        "stops": load_stops(load),
        "rate": load.get("targetRate"),
        "truck": {
            "registration": truck["registration"],
            "bodyType": truck["bodyType"],
            "payloadCapacityKg": truck["payloadCapacityKg"],
            "volumeCapacityM3": truck.get("volumeCapacityM3"),
            "currentLocation": truck["currentLocation"],
        },
    }


def same_point(left, right):
    return abs(float(left["lat"]) - float(right["lat"])) < 0.000001 and abs(
        float(left["lng"]) - float(right["lng"])
    ) < 0.000001


def prepare_offer(db, party, load, truck, match, data):
    require(party.data["verification"] == "VERIFIED", "Account verification is required", 403)
    organisation_users = organisation_user_ids(db, party)
    require(
        load["ownerId"] in organisation_users
        or truck["transporterId"] in organisation_users,
        "Only a matched organisation may prepare terms",
    )
    require(match["status"] in ["SUGGESTED", "OFFERED", "ACCEPTED"], "Match cannot be reviewed", 409)
    require(dt(match["expiresAt"]) > now(), "Match expired", 409)

    # Once the Freight Owner has accepted an allocation, those exact terms are
    # immutable until the transporter confirms or declines them. Re-preparing
    # the dialog must therefore return the accepted revision instead of
    # superseding it with a new OPEN offer (which loses ownerAcceptedAt and
    # makes transporter confirmation fail with "Owner acceptance required").
    previous = sorted(
        [r.data for r in rows(db, "match_offers") if r.data["matchId"] == match["id"]],
        key=lambda item: item["revision"],
        reverse=True,
    )
    if match["status"] == "ACCEPTED":
        accepted = next(
            (
                item
                for item in previous
                if item.get("ownerAcceptedAt")
            ),
            None,
        )
        require(
            accepted is not None,
            "The Freight Owner's accepted terms are unavailable; ask them to review this allocation again",
            409,
        )
        # Repair allocations affected by the former behaviour: preparing the
        # transporter dialog could supersede the accepted revision and create
        # a later OPEN revision. Restore the accepted revision as the only
        # active one so existing engagements remain confirmable after deploy.
        for item in previous:
            if item["id"] != accepted["id"] and item["status"] in ["OPEN", "PARTIALLY_ACCEPTED"]:
                put(db, "match_offers", {**item, "status": "SUPERSEDED"})
        if accepted["status"] != "PARTIALLY_ACCEPTED":
            accepted = {**accepted, "status": "PARTIALLY_ACCEPTED"}
            put(db, "match_offers", accepted)
        quote = needed(db, "route_quotes", accepted["routeQuoteId"]).data
        return {**accepted, "routeQuote": quote}

    journey = verify_route_token(data.journeyRouteToken)
    require(same_point(journey["origin"], load["origin"]), "Journey origin changed", 409)
    require(same_point(journey["destination"], load["destination"]), "Journey destination changed", 409)
    expected_waypoints = [stop["place"] for stop in load_stops(load)[1:-1]]
    supplied_waypoints = journey.get("waypoints", [])
    require(
        len(expected_waypoints) == len(supplied_waypoints)
        and all(same_point(expected, supplied) for expected, supplied in zip(expected_waypoints, supplied_waypoints)),
        "Journey stops changed",
        409,
    )
    deadhead = verify_route_token(data.deadheadRouteToken) if data.deadheadRouteToken else None
    if deadhead:
        require(same_point(deadhead["origin"], truck["currentLocation"]), "Truck location changed", 409)
        require(same_point(deadhead["destination"], load["origin"]), "Pickup location changed", 409)

    business = business_terms(load, truck, match)
    business_hash = stable_hash(business)
    if previous:
        active = previous[0]
        if (
            active["status"] in ["OPEN", "PARTIALLY_ACCEPTED"]
            and active["businessHash"] == business_hash
            and dt(active["expiresAt"]) > now()
        ):
            quote = needed(db, "route_quotes", active["routeQuoteId"]).data
            return {**active, "routeQuote": quote}
        for old in previous:
            if old["status"] in ["OPEN", "PARTIALLY_ACCEPTED"]:
                put(db, "match_offers", {**old, "status": "SUPERSEDED"})

    quote_id = new_id("RQ")
    quote = {
        "id": quote_id,
        "matchId": match["id"],
        "loadId": load["id"],
        "truckId": truck["id"],
        "deadhead": deadhead["route"] if deadhead else None,
        "journey": journey["route"],
        "createdAt": iso(),
        "provider": journey["route"].get("provider", "valhalla"),
    }
    put(db, "route_quotes", quote, load["ownerId"])

    revision = previous[0]["revision"] + 1 if previous else 1
    expires_at = min(
        dt(match["expiresAt"]), now() + timedelta(minutes=settings.match_offer_minutes)
    ).isoformat()
    terms = {**business, "routeQuoteId": quote_id, "routeQuote": quote}
    offer = {
        "id": new_id("OF"),
        "matchId": match["id"],
        "routeQuoteId": quote_id,
        "revision": revision,
        "termsHash": stable_hash(terms),
        "businessHash": business_hash,
        "status": "OPEN",
        "createdAt": iso(),
        "expiresAt": expires_at,
        "ownerAcceptedAt": None,
        "transporterAcceptedAt": None,
        "terms": terms,
    }
    put(db, "match_offers", offer, load["ownerId"])
    audit(db, party, "MATCH_OFFER_PREPARED", "MATCH", match["id"], f"Revision {revision}")
    return {**offer, "routeQuote": quote}


def accepted_offer(db, data, load, truck, match):
    require(data.offerId and data.revision and data.termsHash, "Reviewed offer proof is required", 409)
    offer = needed(db, "match_offers", data.offerId, True).data
    require(offer["matchId"] == match["id"], "Offer does not belong to this match", 409)
    require(offer["revision"] == data.revision, "Offer revision changed", 409)
    require(hmac.compare_digest(offer["termsHash"], data.termsHash), "Offer terms changed", 409)
    require(offer["status"] in ["OPEN", "PARTIALLY_ACCEPTED"], "Offer is no longer active", 409)
    # OPEN terms are short-lived review drafts. Once the Freight Owner accepts
    # them they become the locked allocation terms and remain confirmable until
    # the match itself expires, is declined or is cancelled.
    if not offer.get("ownerAcceptedAt"):
        require(dt(offer["expiresAt"]) > now(), "Offer expired", 409)
    # Before the Freight Owner accepts, compare against live business data so
    # stale terms can never be approved. Afterwards the accepted revision is
    # the contract being confirmed. In particular, GPS updates legitimately
    # change a truck's currentLocation and must not invalidate already accepted
    # terms while the transporter is reviewing them.
    if not offer.get("ownerAcceptedAt"):
        require(
            offer["businessHash"] == stable_hash(business_terms(load, truck, match)),
            "Load or vehicle terms changed; review the new offer",
            409,
        )
    return offer


def receipt(db, party, match, offer, request):
    body = dict(
        id=new_id("AC"),
        matchId=match["id"],
        party=party.data["role"],
        byUserId=party.id,
        at=iso(),
        ip=client_ip(request),
        offerId=offer["id"],
        revision=offer["revision"],
        termsHash=offer["termsHash"],
    )
    body["hash"] = hashlib.sha256(
        json.dumps({**body, "terms": offer["terms"]}, sort_keys=True).encode()
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
    expire_stale_engagements(db)
    load = needed(db, "loads", d["loadId"], True).data
    candidates = [
        r.data
        for r in rows(db, "matches")
        if r.data["loadId"] == load["id"]
        and r.data["status"] in ["SUGGESTED", "OFFERED", "ACCEPTED", "CONFIRMED"]
        and (not d.get("truckId") or r.data["truckPostingId"] == d["truckId"])
    ]
    # A live preview is allowed to discover a newly eligible pair before the
    # periodic suggestion refresh has persisted it. Materialise the exact pair
    # being reviewed here, under the same transaction lock, so the UI can never
    # offer Review and then fail merely because a background refresh has not run.
    if action == "prepareOffer" and not candidates and d.get("truckId"):
        truck = needed(db, "trucks", d["truckId"], True).data
        from app.services.fleet import with_vehicle_verification

        truck = with_vehicle_verification(db, truck)
        organisation_users = organisation_user_ids(db, party)
        require(
            load["ownerId"] in organisation_users
            or truck["transporterId"] in organisation_users,
            "This load and truck are not available to your organisation",
            403,
        )
        require(load["status"] == "POSTED", "Load is no longer available", 409)
        operator = db.get(Party, truck["transporterId"])
        owner = db.get(Party, load["ownerId"])
        require(operator is not None and owner is not None, "Match parties are unavailable", 409)
        config = matching_config(db)
        checked = score(load, truck, operator.data, owner.data, config)
        require(
            checked["passed"],
            "This truck no longer meets the load requirements",
            409,
        )
        require(
            checked["score"] >= config["minimumScore"],
            f"The current match score ({checked['score']}) is below the required minimum ({config['minimumScore']})",
            409,
        )
        match = {
            "id": new_id("MT"),
            "loadId": load["id"],
            "truckPostingId": truck["id"],
            "score": checked["score"],
            "breakdown": checked["breakdown"],
            "hardFilterResults": checked["hardFilterResults"],
            "status": "SUGGESTED",
            "initiatedBy": "SYSTEM",
            "expiresAt": min(
                now() + timedelta(days=1), dt(load["pickupWindow"]["to"])
            ).isoformat(),
            "createdAt": iso(),
        }
        put(db, "matches", match, load["ownerId"])
        candidates = [match]
    candidates.sort(
        key=lambda m: {"CONFIRMED": 0, "ACCEPTED": 1, "OFFERED": 2, "SUGGESTED": 3}[m["status"]]
    )
    require(bool(candidates), "No actionable match found", 404)
    match = candidates[0]
    truck = needed(db, "trucks", match["truckPostingId"], True).data
    from app.services.fleet import with_vehicle_verification

    truck = with_vehicle_verification(db, truck)
    if action == "prepareOffer":
        return prepare_offer(db, party, load, truck, match, data)
    if action == "requestJob":
        require(party.data["verification"] == "VERIFIED", "Account verification is required", 403)
        require(
            truck["transporterId"] in organisation_user_ids(db, party),
            "Only this carrier organisation may request the job",
            403,
        )
        require(load["status"] == "POSTED", "Load is no longer available", 409)
        require(match["status"] == "SUGGESTED", "This job has already been requested", 409)
        require(dt(match["expiresAt"]) > now(), "Match expired", 409)
        checked = score(
            load,
            truck,
            db.get(Party, truck["transporterId"]).data,
            db.get(Party, load["ownerId"]).data,
            matching_config(db),
        )
        require(checked["passed"], "Truck no longer meets the load requirements", 409)
        require(
            checked["score"] >= matching_config(db)["minimumScore"],
            "The match score is below the current minimum",
            409,
        )
        offer = accepted_offer(db, data, load, truck, match)
        require_transition("match", match["status"], "OFFERED")
        requested = {
            **match,
            "status": "OFFERED",
            "initiatedBy": "TRANSPORTER",
            "score": checked["score"],
            "breakdown": checked["breakdown"],
            "hardFilterResults": checked["hardFilterResults"],
            "activeOfferId": offer["id"],
            "offerRevision": offer["revision"],
            "termsHash": offer["termsHash"],
            "requestedAt": iso(),
        }
        put(db, "matches", requested)
        audit(db, party, "JOB_REQUESTED", "MATCH", match["id"], f"{truck['registration']} requested {load['id']}")
        notify(db, load["ownerId"], load["id"], "New carrier job request")
        return {"ok": True, "matchId": match["id"]}
    if action in ["reportIncident", "cancelTrip"]:
        owner = load["ownerId"] in organisation_user_ids(db, party)
        # Cancelling a shipment from the freight-owner side is a load-level
        # operation. A fleet requirement can have several confirmed matches,
        # trips and dispatch assignments; cancelling only the first one leaves
        # the other vehicles incorrectly reserved.
        if action == "cancelTrip" and owner and not d.get("truckId"):
            description = d["reason"].strip()
            require(description, "A cancellation reason is required", 400)
            cancelled_incident_ids = []
            affected_parties = set()
            affected = False
            for match_row in rows(db, "matches"):
                related_match = match_row.data
                if (
                    related_match["loadId"] != load["id"]
                    or related_match["status"] not in ["ACCEPTED", "CONFIRMED"]
                ):
                    continue
                related_truck_row = needed(
                    db, "trucks", related_match["truckPostingId"], True
                )
                related_truck = related_truck_row.data
                related_trip_row = next(
                    (
                        row
                        for row in rows(db, "trips")
                        if row.data["matchId"] == related_match["id"]
                    ),
                    None,
                )
                before_pickup = True
                if related_trip_row and related_trip_row.data["status"] not in [
                    "DELIVERED",
                    "COMPLETED",
                    "CANCELLED",
                ]:
                    related_trip = related_trip_row.data
                    before_pickup = related_trip["status"] in [
                        "SCHEDULED",
                        "EN_ROUTE_PICKUP",
                        "AT_PICKUP",
                    ]
                    incident = dict(
                        id=new_id("IN"),
                        tripId=related_trip["id"],
                        loadId=load["id"],
                        truckId=related_truck["id"],
                        kind="CANCELLATION",
                        description=description,
                        status="OPEN",
                        delayMinutes=None,
                        reportedById=party.id,
                        reportedByRole=party.data["role"],
                        reportedAt=iso(),
                        location=None,
                    )
                    put(db, "incidents", incident, party.id)
                    cancelled_incident_ids.append(incident["id"])
                    require_transition("trip", related_trip["status"], "CANCELLED")
                    put(
                        db,
                        "trips",
                        {
                            **related_trip,
                            "status": "CANCELLED",
                            "operationalStatus": "CANCELLED",
                            "activeIncidentIds": [
                                *related_trip.get("activeIncidentIds", []),
                                incident["id"],
                            ],
                            "events": [
                                *related_trip["events"],
                                {
                                    "status": "CANCELLED",
                                    "at": iso(),
                                    "byUserId": party.id,
                                    "reason": description,
                                },
                            ],
                        },
                    )
                    for dispatch_row in rows(db, "dispatches"):
                        dispatch = dispatch_row.data
                        if (
                            dispatch["tripId"] == related_trip["id"]
                            and dispatch["status"] in ["OFFERED", "ACCEPTED"]
                        ):
                            require_transition("dispatch", dispatch["status"], "CANCELLED")
                            put(
                                db,
                                "dispatches",
                                {
                                    **dispatch,
                                    "status": "CANCELLED",
                                    "respondedAt": iso(),
                                    "timeline": [
                                        *dispatch.get("timeline", []),
                                        {
                                            "status": "CANCELLED",
                                            "at": iso(),
                                            "byUserId": party.id,
                                            "reason": description,
                                        },
                                    ],
                                },
                            )
                if related_match["status"] in ["ACCEPTED", "CONFIRMED"]:
                    require_transition("match", related_match["status"], "WITHDRAWN")
                    put(db, "matches", {**related_match, "status": "WITHDRAWN"})
                    put(
                        db,
                        "trucks",
                        {
                            **related_truck,
                            "status": "AVAILABLE" if before_pickup else "OFFLINE",
                        },
                    )
                    affected = True
                    affected_parties.add(related_truck["transporterId"])
                    if related_truck.get("driverId"):
                        affected_parties.add(related_truck["driverId"])
            require(affected, "No active shipment assignment was found", 409)
            if load["status"] != "CANCELLED":
                require_transition("load", load["status"], "CANCELLED")
            put(db, "loads", {**load, "status": "CANCELLED"})
            for party_id in affected_parties - {party.id}:
                notify(db, party_id, load["id"], "Shipment cancelled")
            audit(
                db,
                party,
                "LOAD_CANCELLED",
                "LOAD",
                load["id"],
                description,
            )
            return {
                "ok": True,
                "incidentId": cancelled_incident_ids[0]
                if cancelled_incident_ids
                else None,
            }
        trip = next(
            (r.data for r in rows(db, "trips") if r.data["matchId"] == match["id"]),
            None,
        )
        require(trip is not None, "Trip not found", 404)
        require(trip["status"] not in ["DELIVERED", "COMPLETED", "CANCELLED"], "Trip is closed", 409)
        assigned = party.id == truck.get("driverId")
        carrier = (
            party.data.get("role") == "TRANSPORTER"
            and truck["transporterId"] in organisation_user_ids(db, party)
        )
        owner = load["ownerId"] in organisation_user_ids(db, party)
        require(
            assigned or carrier or owner,
            "Only a trip party may report an operational issue",
        )
        if action == "cancelTrip":
            require(
                owner or carrier or assigned,
                "Only the load owner, carrier or assigned driver may cancel this trip",
            )
            kind, description = "CANCELLATION", d["reason"].strip()
        else:
            kind, description = d["kind"], d["description"].strip()
            if kind == "DELAY":
                require(d.get("delayMinutes") is not None, "Delay duration is required", 400)
            if kind == "NO_SHOW":
                entered = trip.get("geofences", {}).get("PICKUP", {}).get("enteredAt")
                require(
                    entered and now() - dt(entered) >= timedelta(minutes=30),
                    "No-show requires 30 minutes in the pickup geofence",
                    409,
                )
        latest = db.scalar(
            select(VehiclePosition)
            .where(VehiclePosition.trip_id == trip["id"])
            .order_by(VehiclePosition.recorded_at.desc())
            .limit(1)
        )
        incident = dict(
            id=new_id("IN"),
            tripId=trip["id"],
            loadId=load["id"],
            truckId=truck["id"],
            kind=kind,
            description=description,
            status="OPEN",
            delayMinutes=d.get("delayMinutes"),
            reportedById=party.id,
            reportedByRole=party.data["role"],
            reportedAt=iso(),
            location=(
                {
                    "lat": latest.lat,
                    "lng": latest.lng,
                    "recordedAt": latest.recorded_at.isoformat(),
                }
                if latest
                else None
            ),
        )
        put(db, "incidents", incident, party.id)
        counterpart_ids = {load["ownerId"], truck["transporterId"]} - {party.id}
        if truck.get("driverId"):
            counterpart_ids.add(truck["driverId"])
            counterpart_ids.discard(party.id)
        if action == "cancelTrip":
            before_pickup = trip["status"] in ["SCHEDULED", "EN_ROUTE_PICKUP", "AT_PICKUP"]
            carrier_side = carrier or assigned
            require_transition("trip", trip["status"], "CANCELLED")
            require_transition("match", match["status"], "WITHDRAWN")
            put(
                db,
                "trips",
                {
                    **trip,
                    "status": "CANCELLED",
                    "operationalStatus": "CANCELLED",
                    "activeIncidentIds": [
                        *trip.get("activeIncidentIds", []),
                        incident["id"],
                    ],
                    "events": [
                        *trip["events"],
                        {
                            "status": "CANCELLED",
                            "at": iso(),
                            "byUserId": party.id,
                            "reason": description,
                        },
                    ],
                },
            )
            put(
                db,
                "matches",
                {
                    **match,
                    "status": "WITHDRAWN",
                    "excludedFromRematch": True,
                    "withdrawalReason": description,
                },
            )
            other_selected = any(
                row.data["loadId"] == load["id"]
                and row.data["id"] != match["id"]
                and row.data["status"] in ["ACCEPTED", "CONFIRMED"]
                for row in rows(db, "matches")
            )
            target_load_status = (
                "MATCHED"
                if (owner and d.get("truckId")) or (carrier_side and before_pickup and other_selected)
                else "POSTED"
                if carrier_side and before_pickup
                else "CANCELLED"
            )
            if load["status"] != target_load_status:
                require_transition("load", load["status"], target_load_status)
            put(
                db,
                "loads",
                {**load, "status": target_load_status},
            )
            put(db, "trucks", {**truck, "status": "AVAILABLE" if before_pickup else "OFFLINE"})
            if target_load_status in ["POSTED", "MATCHED"]:
                from app.services.domain import refresh_suggestions

                refresh_suggestions(db)
            event, title = "TRIP_CANCELLED", "Trip cancelled"
        else:
            op = "DELAYED" if kind in ["DELAY", "ROUTE_OBSTRUCTION"] else "ACTION_REQUIRED"
            eta = trip.get("etaAt")
            if eta and d.get("delayMinutes"):
                eta = (dt(eta) + timedelta(minutes=d["delayMinutes"])).isoformat()
            put(
                db,
                "trips",
                {
                    **trip,
                    "operationalStatus": op,
                    "activeIncidentIds": [
                        *trip.get("activeIncidentIds", []),
                        incident["id"],
                    ],
                    "etaAt": eta,
                },
            )
            event, title = "TRIP_INCIDENT_REPORTED", f"{kind.replace('_', ' ').title()} reported"
        audit(db, party, event, "TRIP", trip["id"], description)
        for recipient in counterpart_ids:
            notify(db, recipient, load["id"], title)
        return {"ok": True, "incidentId": incident["id"]}
    if action == "advanceTrip":
        assigned_driver = truck.get("driverId")
        trip = next((r.data for r in rows(db, "trips") if r.data["matchId"] == match["id"]), None)
        require(trip is not None and trip["status"] in STEPS[:-1], "Trip cannot advance", 409)
        dispatches = [r.data for r in rows(db, "dispatches") if r.data["tripId"] == trip["id"]]
        if dispatches:
            require(
                assigned_driver is not None
                and any(
                    item["driverId"] == assigned_driver and item["status"] == "ACCEPTED"
                    for item in dispatches
                ),
                "The assigned driver must accept this dispatch first",
                409,
            )
        require(
            party.id == assigned_driver
            if assigned_driver
            else party.id == truck["transporterId"] and party.data["role"] == "TRANSPORTER",
            "Only the assigned driver may advance this trip",
        )
        stops = load_stops(load)
        status, stop_index = next_trip_step(trip, stops)
        require_transition("trip", trip["status"], status)
        current_stop = stops[stop_index]
        zone = "PICKUP" if current_stop["kind"] == "PICKUP" else "DESTINATION"
        geofence = trip.get("geofences", {}).get("CURRENT_STOP") or trip.get("geofences", {}).get(zone, {})
        if status in ["AT_PICKUP", "AT_DROPOFF"]:
            require(
                geofence.get("inside"),
                f"Enter the {current_stop['kind'].lower()} geofence before confirming arrival",
                409,
            )
            stops[stop_index] = {**current_stop, "status": "ARRIVED", "arrivedAt": iso()}
            current_stop = stops[stop_index]
        if status in ["LOADED", "DELIVERED"]:
            proof = d.get("proof")
            kind = "PICKUP" if status == "LOADED" else "DELIVERY"
            zone = "PICKUP" if kind == "PICKUP" else "DESTINATION"
            require(
                proof is not None and proof.get("recipientName", "").strip(),
                f"Proof of {kind.lower()} requires a named contact",
                400,
            )
            require(
                geofence.get("inside"),
                f"Proof of {kind.lower()} must be captured inside the {zone.lower()} geofence",
                409,
            )
            latest = db.scalar(
                select(VehiclePosition)
                .where(VehiclePosition.trip_id == trip["id"])
                .order_by(VehiclePosition.recorded_at.desc(), VehiclePosition.id.desc())
                .limit(1)
            )
            require(
                latest is not None and now() - latest.received_at <= timedelta(minutes=5),
                "A recent verified GPS position is required for proof capture",
                409,
            )
            if proof.get("photoUrl"):
                from app.services.storage import own_upload

                own_upload(db, party, proof["photoUrl"], "POD")
            proof_id = new_id("PF")
            put(
                db,
                "proofs",
                dict(
                    **proof,
                    id=proof_id,
                    stopId=current_stop["id"],
                    tripId=trip["id"],
                    loadId=load["id"],
                    kind=kind,
                    capturedAt=iso(),
                    location={
                        "lat": latest.lat,
                        "lng": latest.lng,
                        "accuracyM": latest.accuracy_m,
                        "recordedAt": latest.recorded_at.isoformat(),
                        "verified": True,
                    },
                ),
                party.id,
            )
            stops[stop_index] = {**current_stop, "status": "COMPLETED", "completedAt": iso(), "proofId": proof_id}
        # The final delivery proof is the authoritative close-out event. Do not
        # leave a direct or final-stop trip parked at DELIVERED/90% waiting for
        # a second, non-operational button press. Preserve both lifecycle
        # events for the audit timeline, then finish the trip atomically.
        event_at = iso()
        transition_events = [{"status": status, "at": event_at, "byUserId": party.id}]
        if status == "DELIVERED" and stop_index == len(stops) - 1:
            require_transition("trip", "DELIVERED", "COMPLETED")
            status = "COMPLETED"
            transition_events.append(
                {"status": "COMPLETED", "at": event_at, "byUserId": party.id}
            )

        load = {**load, "stops": stops}
        put(db, "loads", load)
        completed_stops = sum(stop["status"] == "COMPLETED" for stop in stops)
        progress = 100 if status == "COMPLETED" else round(completed_stops / len(stops) * 90 + (5 if status in ["AT_PICKUP", "AT_DROPOFF"] else 0))
        trip = {
            **trip,
            "status": status,
            "currentStopIndex": stop_index,
            "currentStopId": stops[stop_index]["id"],
            "progressPct": progress,
            "events": [*trip["events"], *transition_events],
            "suggestedAction": None,
        }
        put(db, "trips", trip)
        put(
            db,
            "trucks",
            {
                **truck,
                "status": (
                    "AVAILABLE" if truck.get("alwaysAvailable") else "OFFLINE"
                )
                if status == "COMPLETED"
                else "ON_TRIP",
            },
        )
        if status == "COMPLETED":
            put(db, "loads", {**load, "status": "COMPLETED"})
            from app.services.finance import ensure_invoice_for_trip

            invoice = ensure_invoice_for_trip(db, trip, load, truck, match)
            audit(
                db,
                party,
                "INVOICE_ISSUED",
                "INVOICE",
                invoice["id"],
                f"{invoice['invoiceNumber']} issued for ZAR {invoice['total']:.2f}",
            )
        audit(db, party, "TRIP_STATUS_CHANGED", "TRIP", trip["id"], status)
        from app.services.messaging import conversation_for_trip, system_message

        conversation = conversation_for_trip(db, trip["id"])
        if conversation:
            system_message(
                db,
                conversation,
                f"Trip status changed to {status.replace('_', ' ').title()}.",
                "TRIP_STATUS",
            )
        notify(db, load["ownerId"], load["id"], f"Trip {status.lower().replace('_', ' ')}")
        return {"ok": True, "status": status, "progressPct": progress}
    require(party.data["verification"] == "VERIFIED", "Account verification is required", 403)
    organisation_users = organisation_user_ids(db, party)
    trucks_required = max(1, int(load.get("trucksRequired") or 1))
    load_matches = [m.data for m in rows(db, "matches") if m.data["loadId"] == load["id"]]
    selected_matches = [
        item for item in load_matches if item["status"] in ["ACCEPTED", "CONFIRMED"]
    ]
    if action == "confirmMatch":
        require(
            truck["transporterId"] in organisation_users,
            "Only this carrier organisation may confirm",
        )
        require(
            match["status"] == "ACCEPTED" and load["status"] in ["MATCHED", "CONFIRMED"],
            "Owner acceptance required",
            409,
        )
    else:
        require(load["ownerId"] in organisation_users, "Only the freight owner organisation may accept")
        require(load["status"] in ["POSTED", "MATCHED"], "Load is no longer available", 409)
        require(
            len(selected_matches) < trucks_required,
            "The required number of trucks has already been selected",
            409,
        )
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
        matching_config(db),
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
    offer = accepted_offer(db, data, load, truck, match)
    if action == "confirmMatch":
        require(offer.get("ownerAcceptedAt"), "Owner acceptance required", 409)
    else:
        require(not offer.get("ownerAcceptedAt"), "Owner already accepted this offer", 409)
    final = action == "confirmMatch"
    target_match_status = "CONFIRMED" if final else "ACCEPTED"
    require_transition("match", match["status"], target_match_status)
    selected_after = len(selected_matches) + (0 if final else 1)
    confirmed_before = sum(item["status"] == "CONFIRMED" for item in load_matches)
    confirmed_after = confirmed_before + (1 if final else 0)
    target_load_status = (
        "CONFIRMED" if final and confirmed_after >= trucks_required else "MATCHED"
    )
    if load["status"] != target_load_status:
        require_transition("load", load["status"], target_load_status)
    match = {
        **match,
        "status": target_match_status,
        "agreedRate": load["targetRate"],
        "activeOfferId": offer["id"],
        "offerRevision": offer["revision"],
        "termsHash": offer["termsHash"],
    }
    match["confirmedByTransporterAt" if action == "confirmMatch" else "confirmedByOwnerAt"] = iso()
    put(db, "matches", match)
    accepted_at = iso()
    offer = {
        **offer,
        "status": "CONFIRMED" if final else "PARTIALLY_ACCEPTED",
        "transporterAcceptedAt" if final else "ownerAcceptedAt": accepted_at,
        # An owner-approved allocation must not expire on the short draft-offer
        # timer while its transporter is still reviewing it.
        "expiresAt": offer["expiresAt"] if final else match["expiresAt"],
    }
    put(db, "match_offers", offer)
    receipt(db, party, match, offer, request)
    put(db, "loads", {**load, "status": target_load_status})
    put(db, "trucks", {**truck, "status": "RESERVED"})
    # A fleet requirement stays open while trucks are being selected. Only
    # withdraw the unused candidates once every required slot has been filled.
    if not final and selected_after >= trucks_required:
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
        assigned_driver = (
            db.get(Party, truck.get("driverId")) if truck.get("driverId") else None
        )
        truck_row = needed(db, "trucks", truck["id"])
        organisation_id = truck_row.organisation_id
        owner_driver = bool(
            assigned_driver
            and assigned_driver.id == truck["transporterId"]
            and assigned_driver.data["role"] == "TRANSPORTER"
        )
        driver_membership = (
            db.scalar(
                select(OrganisationMembership).where(
                    OrganisationMembership.organisation_id == organisation_id,
                    OrganisationMembership.user_id == assigned_driver.id,
                    OrganisationMembership.status == "ACTIVE",
                )
            )
            if assigned_driver and organisation_id
            else None
        )
        driver_ready = bool(
            assigned_driver
            and assigned_driver.data.get("verification") == "VERIFIED"
            and (assigned_driver.data.get("role") == "DRIVER" or owner_driver)
            and (owner_driver or driver_membership is not None)
        )
        if driver_ready:
            from app.services.driver_compliance import require_dispatch_compliance

            try:
                require_dispatch_compliance(db, assigned_driver, organisation_id)
            except AppError:
                driver_ready = False

        dispatch_status = (
            "ASSIGNED" if driver_ready and owner_driver else
            "AWAITING_DRIVER" if driver_ready else
            "NEEDS_DRIVER"
        )
        trip = dict(
            id=trip_id,
            matchId=match["id"],
            status="SCHEDULED",
            dispatchStatus=dispatch_status,
            assignedDriverId=assigned_driver.id if driver_ready else None,
            events=[dict(status="SCHEDULED", at=iso(), byUserId=party.id)],
            livePosition=None,
            simulatedPosition=None,
            progressPct=0,
            currentStopIndex=0,
            currentStopId=load_stops(load)[0]["id"],
            etaAt=None,
            geofences={
                "PICKUP": {"inside": False},
                "DESTINATION": {"inside": False},
            },
            geofenceEvents=[],
            suggestedAction=None,
        )
        put(db, "trips", trip, load["ownerId"])
        if driver_ready:
            assigned_at = iso()
            status = "ACCEPTED" if owner_driver else "OFFERED"
            dispatch = {
                "id": new_id("DP"),
                "tripId": trip_id,
                "loadId": load["id"],
                "truckId": truck["id"],
                "carrierId": truck["transporterId"],
                "driverId": assigned_driver.id,
                "status": status,
                "offeredAt": assigned_at,
                "respondedAt": assigned_at if owner_driver else None,
                "note": "Automatically assigned from the selected truck",
                "timeline": [{"status": status, "at": assigned_at, "byUserId": party.id}],
            }
            put(db, "dispatches", dispatch, truck["transporterId"])
            notify(
                db,
                assigned_driver.id,
                load["id"],
                "Job ready" if owner_driver else "New delivery assignment",
            )
        from app.services.messaging import ensure_conversation, system_message

        conversation = ensure_conversation(db, trip_id, load, truck)
        system_message(
            db,
            conversation,
            "Engagement confirmed. Trip scheduled.",
            "MATCH_CONFIRMED",
        )
        return {"ok": True, "tripId": trip_id}
    return {"ok": True}
