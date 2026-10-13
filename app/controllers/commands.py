import hashlib
import hmac
import json
from datetime import timedelta

from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.errors import require
from app.core.security import iso, new_id, now
from app.db.models import Party
from app.services.domain import audit, lock_domain, truck_access
from app.services.matching import dt, score
from app.services.maps import verify_route_token
from app.services.records import needed, put, rows

STEPS = ["SCHEDULED", "AT_PICKUP", "LOADED", "IN_TRANSIT", "AT_DROPOFF", "DELIVERED", "COMPLETED"]


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
    require(
        party.id in [load["ownerId"], truck["transporterId"]],
        "Only a matched party may prepare terms",
    )
    require(match["status"] in ["SUGGESTED", "OFFERED", "ACCEPTED"], "Match cannot be reviewed", 409)
    require(dt(match["expiresAt"]) > now(), "Match expired", 409)

    journey = verify_route_token(data.journeyRouteToken)
    require(same_point(journey["origin"], load["origin"]), "Journey origin changed", 409)
    require(same_point(journey["destination"], load["destination"]), "Journey destination changed", 409)
    deadhead = verify_route_token(data.deadheadRouteToken) if data.deadheadRouteToken else None
    if deadhead:
        require(same_point(deadhead["origin"], truck["currentLocation"]), "Truck location changed", 409)
        require(same_point(deadhead["destination"], load["origin"]), "Pickup location changed", 409)

    business = business_terms(load, truck, match)
    business_hash = stable_hash(business)
    previous = sorted(
        [r.data for r in rows(db, "match_offers") if r.data["matchId"] == match["id"]],
        key=lambda item: item["revision"],
        reverse=True,
    )
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
    require(dt(offer["expiresAt"]) > now(), "Offer expired", 409)
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
    if action == "prepareOffer":
        return prepare_offer(db, party, load, truck, match, data)
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
    offer = accepted_offer(db, data, load, truck, match)
    if action == "confirmMatch":
        require(offer.get("ownerAcceptedAt"), "Owner acceptance required", 409)
    else:
        require(not offer.get("ownerAcceptedAt"), "Owner already accepted this offer", 409)
    final = action == "confirmMatch"
    match = {
        **match,
        "status": "CONFIRMED" if final else "ACCEPTED",
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
    }
    put(db, "match_offers", offer)
    receipt(db, party, match, offer, request)
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
