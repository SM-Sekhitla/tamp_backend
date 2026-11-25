from datetime import timedelta
from io import BytesIO
from uuid import uuid4

from PIL import Image
from sqlalchemy import select

from app.controllers.auth import create_party
from app.core.security import new_id, now
from app.db.models import Organisation, OrganisationMembership, UserRole
from app.schemas.domain import Place, RouteInput
from app.services.maps import _estimated_route
from app.services.rbac import ROLE_IDS, primary_organisation_id
from app.services.records import needed, put

from .conftest import db_call, login, result, rpc
from .test_workflow import DEST, PLACE


def test_minibus_registration_review_and_publication(client, accounts):
    def setup(db):
        org = db.scalar(select(OrganisationMembership.organisation_id).where(
            OrganisationMembership.user_id == accounts["carrier"]["id"],
            OrganisationMembership.is_primary.is_(True),
        ))
        db.get(Organisation, org).verification = "VERIFIED"
        db.info["active_organisation_id"] = org
        for document in ("PRDP", "DRIVING_LICENCE"):
            put(db, "driverDocuments", {
                "id": new_id("DD"), "driverId": accounts["driver"]["id"],
                "documentType": document, "reviewStatus": "APPROVED",
                "expiresAt": (now() + timedelta(days=30)).isoformat(),
            }, accounts["carrier"]["id"])

    db_call(setup)
    login(client, "carrier")
    image = BytesIO()
    Image.new("RGB", (16, 16), "yellow").save(image, "PNG")

    def upload(kind, name):
        response = client.post("/api/uploads", data={"kind": kind},
                               files={"file": (name + ".png", image.getvalue(), "image/png")})
        assert response.status_code == 200, response.text
        return response.json()["url"]

    photo = upload("PASSENGER_PHOTO", "minibus")
    documents = {
        key: upload("PASSENGER_DOCUMENT", key)
        for key in ("licence", "roadworthy", "operatingLicence", "insurance")
    }
    start = now() + timedelta(days=1)
    expiry = (start + timedelta(days=30)).date().isoformat()
    values = {
        "type": "MINIBUS", "subtype": "COMMUTER_TAXI", "make": "Toyota", "model": "Hiace", "registration": "AB 12 CD GP",
        "seats": 15, "wheelchairPositions": 1, "displacedSeats": 2,
        "location": PLACE, "serviceArea": "Gauteng",
        "availability": {"start": start.isoformat(), "end": (start + timedelta(days=7)).isoformat()},
        "licenceExpiry": expiry, "permitExpiry": expiry, "photos": [photo], "documents": documents,
        "driverId": accounts["driver"]["id"],
    }
    assert rpc(client, "passenger.save", {**values, "subtype": "HIGH_DECK"}, True).status_code == 400
    saved = result(rpc(client, "passenger.save", values, True))
    assert saved["verification"] == "PENDING"
    assert rpc(client, "passenger.save", values, True).status_code == 409
    fleet = result(rpc(client, "passenger.fleet"))
    assert fleet["drivers"][0]["id"] == accounts["driver"]["id"]
    update = {**values, "id": saved["id"], "version": fleet["vehicles"][0]["version"], "published": True}
    assert rpc(client, "passenger.save", update, True).status_code == 409
    login(client, "owner")
    assert result(rpc(client, "passenger.catalogue")) == []
    assert client.get(photo).status_code == 403
    assert rpc(client, "passenger.reviews").status_code == 403
    login(client, "admin")
    assert client.get(documents["licence"]).status_code == 200
    result(rpc(client, "passenger.review", {
        "id": saved["id"], "version": update["version"], "status": "VERIFIED",
    }, True))
    login(client, "carrier")
    assert rpc(client, "passenger.save", update, True).status_code == 409
    update["version"] = result(rpc(client, "passenger.fleet"))["vehicles"][0]["version"]
    assert result(rpc(client, "passenger.save", update, True))["published"]
    login(client, "owner")
    [listing] = result(rpc(client, "passenger.catalogue"))
    assert listing["seats"] == 15
    assert listing["subtype"] == "COMMUTER_TAXI"
    assert listing["subtypeLabel"] == "Commuter minibus taxi"
    assert not {"documents", "registration", "driverId"} & listing.keys()
    assert client.get(photo).status_code == 200
    assert client.get(documents["licence"]).status_code == 403
    exercise_charter_booking(client, listing, start)
    exercise_combined_charter(client, listing, start)
    login(client, "carrier")
    update["version"] = result(rpc(client, "passenger.fleet"))["vehicles"][0]["version"]
    assert result(rpc(client, "passenger.save", {**update, "seats": 14}, True))["verification"] == "PENDING"
    login(client, "owner")
    assert all(row["id"] != saved["id"] for row in result(rpc(client, "passenger.catalogue")))
    assert client.get(photo).status_code == 403


def exercise_charter_booking(client, listing, availability_start):
    pickup = availability_start + timedelta(hours=3)
    stop = {**PLACE, "lat": (PLACE["lat"] + DEST["lat"]) / 2, "label": "Intermediate stop"}
    route = _estimated_route(RouteInput(origin=Place(**PLACE), destination=Place(**DEST), waypoints=[Place(**stop)]))
    return_route = _estimated_route(RouteInput(origin=Place(**DEST), destination=Place(**PLACE)))
    leg = {
        "origin": PLACE, "destination": DEST, "stops": [stop], "pickupAt": pickup.isoformat(),
        "passengers": 14, "wheelchairPositions": 1, "routeToken": route["routeToken"],
    }
    request = {
        "id": str(uuid4()), "vehicleId": listing["id"], "outbound": leg,
        "returnLeg": {
            **leg, "origin": DEST, "destination": PLACE, "stops": [], "passengers": 12,
            "pickupAt": (pickup + timedelta(hours=8)).isoformat(), "routeToken": return_route["routeToken"],
        },
        "contactName": "Test organiser", "contactPhone": "0821234567",
    }
    search = {key: request[key] for key in ("outbound", "returnLeg")}
    [provider] = result(rpc(client, "charter.search", search, True))
    assert provider["vehicleId"] == listing["id"]
    assert provider["vehicles"][0]["seats"] == 15
    assert "driverId" not in provider["vehicles"][0]
    assert result(rpc(client, "charter.search", {
        **search, "outbound": {**leg, "passengers": 16},
    }, True)) == []
    request["requireFleetMatch"] = True
    booking = result(rpc(client, "charter.request", request, True))
    assert result(rpc(client, "charter.request", request, True))["id"] == booking["id"]
    conflicting = result(rpc(client, "charter.request", {**request, "id": str(uuid4())}, True))
    login(client, "other")
    assert result(rpc(client, "charter.list")) == []
    assert rpc(client, "charter.transition", {
        "id": booking["id"], "version": booking["version"], "action": "CANCEL", "reason": "Other tenant",
    }, True).status_code == 404

    login(client, "carrier")
    assert {row["id"] for row in result(rpc(client, "charter.list"))} == {booking["id"], conflicting["id"]}
    terms = {
        "vehicleIds": [listing["id"]], "total": "4500.00", "vat": "INCL",
        "start": (pickup - timedelta(hours=1)).isoformat(),
        "end": (pickup + timedelta(hours=12)).isoformat(),
        "validUntil": (pickup - timedelta(hours=2)).isoformat(),
    }
    recommendation = {
        "id": booking["id"], "version": booking["version"],
        "start": terms["start"], "end": terms["end"],
    }
    assert result(rpc(client, "charter.recommend", recommendation))["vehicleIds"] == [listing["id"]]
    assert rpc(client, "charter.quote", {
        **terms, "id": booking["id"], "version": booking["version"],
        "end": (pickup + timedelta(hours=1)).isoformat(),
    }, True).status_code == 409
    first, second = [
        result(rpc(client, "charter.quote", {**terms, "id": row["id"], "version": row["version"]}, True))
        for row in (booking, conflicting)
    ]
    login(client, "owner")
    confirmed = result(rpc(client, "charter.transition", {
        "id": first["id"], "version": first["version"], "action": "CONFIRM",
    }, True))
    assert confirmed["status"] == "CONFIRMED"
    response = {"bookingId": first["id"], "vehicleId": listing["id"], "action": "ACCEPT"}
    login(client, "other")
    assert rpc(client, "charter.driverJobs").status_code == 403
    login(client, "driver")
    [job] = result(rpc(client, "charter.driverJobs"))
    assert job["status"] == "OFFERED"
    assert job["itinerary"]["outbound"]["assigned"] == {"passengers": 14, "wheelchairPositions": 1}
    assert job["itinerary"]["returnLeg"]["assigned"]["passengers"] == 12
    assert "quote" not in job and "contactPhone" not in job
    assert rpc(client, "charter.respondJob", {**response, "vehicleId": "other"}, True).status_code == 404
    assert result(rpc(client, "charter.respondJob", response, True))["status"] == "ACCEPTED"
    assert result(rpc(client, "charter.respondJob", response, True))["status"] == "ACCEPTED"

    def advance(leg, revision, action):
        return rpc(client, "charter.advanceJob", {
            "bookingId": first["id"], "vehicleId": listing["id"], "leg": leg,
            "action": action, "revision": revision,
        }, True)

    def publish(leg):
        return rpc(client, "charter.publishPosition", {
            "bookingId": first["id"], "vehicleId": listing["id"], "leg": leg,
            "lat": PLACE["lat"], "lng": PLACE["lng"], "accuracyM": 8,
            "recordedAt": now().isoformat(),
        }, True)

    assert publish("outbound").status_code == 409
    assert advance("outbound", 0, "START").status_code == 409

    def open_reservation(db):
        row = needed(db, "charterBookings", first["id"])
        row.data = {**row.data, "quote": {**row.data["quote"],
                    "start": (now() - timedelta(minutes=1)).isoformat()}}

    db_call(open_reservation)
    assert advance("returnLeg", 0, "START").status_code == 409
    assert advance("outbound", 0, "BOARD").status_code == 409
    expected = ("TO_PICKUP", "AT_PICKUP", "ON_ROUTE", "ON_ROUTE", "COMPLETED")
    for revision, action in enumerate(("START", "ARRIVE_PICKUP", "BOARD", "COMPLETE_STOP", "COMPLETE")):
        if action == "COMPLETE_STOP":
            assert advance("outbound", revision, "COMPLETE").status_code == 409
        progress = result(advance("outbound", revision, action))["itinerary"]["outbound"]["progress"]
        assert progress["stage"] == expected[revision]
        if action == "START":
            assert publish("returnLeg").status_code == 409
            sent = result(publish("outbound"))
            assert result(publish("outbound")) == sent
            login(client, "other")
            assert rpc(client, "charter.live", {"bookingId": first["id"]}).status_code == 404
            login(client, "owner")
            [vehicle] = result(rpc(client, "charter.live", {"bookingId": first["id"]}))
            assert vehicle["activeLeg"] == "outbound"
            assert vehicle["position"]["recordedAt"] == sent["recordedAt"]
            assert vehicle["position"]["accuracyM"] == 8
            login(client, "driver")
        assert progress["revision"] == revision + 1
        assert result(advance("outbound", revision, action))["itinerary"]["outbound"]["progress"] == progress
    login(client, "owner")
    [outbound_done] = [row for row in result(rpc(client, "charter.list")) if row["id"] == first["id"]]
    [vehicle_progress] = outbound_done["vehicleProgress"]
    assert vehicle_progress["legs"]["outbound"]["stage"] == "COMPLETED"
    [vehicle] = result(rpc(client, "charter.live", {"bookingId": first["id"]}))
    assert vehicle["activeLeg"] is None and vehicle["position"] is None
    assert vehicle_progress["legs"]["returnLeg"]["stage"] == "SCHEDULED"
    login(client, "driver")
    for revision, action in enumerate(("START", "ARRIVE_PICKUP", "BOARD", "COMPLETE")):
        returned = result(advance("returnLeg", revision, action))
    assert returned["itinerary"]["returnLeg"]["progress"]["stage"] == "COMPLETED"
    login(client, "owner")
    [booked] = [row for row in result(rpc(client, "charter.list")) if row["id"] == first["id"]]
    assert "driverJobs" not in booked and "driverAssignments" not in booked
    assert result(rpc(client, "charter.search", search, True)) == []
    assert rpc(client, "charter.request", {**request, "id": str(uuid4())}, True).status_code == 409
    login(client, "carrier")
    [booked] = [row for row in result(rpc(client, "charter.list")) if row["id"] == first["id"]]
    assert booked["driverAssignments"][0]["status"] == "ACCEPTED"
    assert result(rpc(client, "charter.recommend", {
        **recommendation, "id": second["id"], "version": second["version"],
    }))["vehicleIds"] == []
    login(client, "owner")
    assert confirmed["quote"]["total"] == "4500.00"
    assert "driverId" not in confirmed["quote"]["vehicles"][0]
    action = {"id": second["id"], "version": second["version"], "action": "CONFIRM"}
    assert rpc(client, "charter.transition", action, True).status_code == 409
    result(rpc(client, "charter.transition", {
        "id": first["id"], "version": confirmed["version"], "action": "CANCEL", "reason": "Event cancelled",
    }, True))
    replacement = result(rpc(client, "charter.transition", action, True))
    result(rpc(client, "charter.transition", {
        "id": second["id"], "version": replacement["version"], "action": "CANCEL", "reason": "Event cancelled",
    }, True))


def exercise_combined_charter(client, listing, availability_start):
    def seed(db):
        original = needed(db, "vehicles", listing["id"]).data
        for index in (2, 3):
            provider = create_party(db, f"bus{index}@example.com", "Testing-password-123",
                                    "TRANSPORTER", f"Bus {index}", f"Bus Company {index}")
            provider.data = {**provider.data, "verification": "VERIFIED"}
            driver = create_party(db, f"busdriver{index}@example.com", "Testing-password-123",
                                  "DRIVER", f"Bus driver {index}", f"Driver {index}")
            driver.data = {**driver.data, "verification": "VERIFIED"}
            org = primary_organisation_id(db, provider)
            db.get(Organisation, org).verification = "VERIFIED"
            db.add(OrganisationMembership(id=new_id("OM"), organisation_id=org, user_id=driver.id,
                                          status="ACTIVE", is_primary=False, created_at=now()))
            db.add(UserRole(id=new_id("UR"), user_id=driver.id, role_id=ROLE_IDS["DRIVER"],
                            organisation_id=org, assigned_by=provider.id, created_at=now()))
            db.info["active_organisation_id"] = org
            for doc in ("PRDP", "DRIVING_LICENCE"):
                put(db, "driverDocuments", {
                    "id": new_id("DD"), "driverId": driver.id, "documentType": doc, "reviewStatus": "APPROVED",
                    "expiresAt": (now() + timedelta(days=30)).isoformat(),
                }, provider.id)
            put(db, "vehicles", {
                **original, "id": new_id("VH"), "driverId": driver.id, "registration": f"BUS{index}",
                "seats": 12 if index == 2 else 10, "photos": [], "documents": {},
                "ownerId": provider.id,
            }, provider.id)

    db_call(seed)
    pickup = availability_start + timedelta(hours=3)
    route = _estimated_route(RouteInput(origin=Place(**PLACE), destination=Place(**DEST)))
    trip = {
        "outbound": {"origin": PLACE, "destination": DEST, "pickupAt": pickup.isoformat(),
                     "passengers": 23, "routeToken": route["routeToken"]},
        "allowSplit": True, "allowMultipleProviders": True,
    }
    login(client, "owner")
    assert result(rpc(client, "charter.search", {**trip, "allowMultipleProviders": False}, True)) == []
    [gap] = result(rpc(client, "charter.search", {
        **trip, "outbound": {**trip["outbound"], "passengers": 40},
    }, True))
    assert gap["gaps"][0]["passengers"] == 3
    [combined] = result(rpc(client, "charter.search", trip, True))
    assert len(combined["allocations"]) == 2
    request = {
        **trip, "id": str(uuid4()), "vehicleId": combined["vehicleId"],
        "vehicleIds": [vehicle["id"] for vehicle in combined["vehicles"]],
        "contactName": "Organiser", "contactPhone": "0821234567",
    }
    group = result(rpc(client, "charter.requestGroup", request, True))["id"]
    assert result(rpc(client, "charter.requestGroup", request, True))["id"] == group

    def allocations():
        return [row for row in result(rpc(client, "charter.list")) if row.get("groupId") == group]

    def action(rows, verb="CONFIRM"):
        return {"id": group, "versions": {row["id"]: row["version"] for row in rows},
                "action": verb, "reason": "Change of plan"}

    assert len(allocations()) == 2
    assert rpc(client, "charter.transitionGroup", action(allocations()), True).status_code == 409
    login(client, "bus2")
    [declined] = allocations()
    result(rpc(client, "charter.transition", {
        "id": declined["id"], "version": declined["version"], "action": "DECLINE", "reason": "Conflict",
    }, True))

    login(client, "owner")
    target = next(row for row in allocations() if row["id"] == declined["id"])
    offer = {"id": target["id"], "version": target["version"]}
    [option] = result(rpc(client, "charter.replacementOptions", offer))
    assert option["supplier"] == "Bus Company 3"
    result(rpc(client, "charter.replaceProvider", {**offer, "vehicleIds": [option["vehicleId"]]}, True))
    login(client, "bus2")
    assert allocations() == []

    for account in ("carrier", "bus3"):
        login(client, account)
        [row] = allocations()
        result(rpc(client, "charter.quote", {
            "id": row["id"], "version": row["version"], "vehicleIds": [row["vehicleId"]],
            "total": "2500.00", "vat": "INCL", "start": pickup.isoformat(),
            "end": (pickup + timedelta(hours=3)).isoformat(),
            "validUntil": (pickup - timedelta(hours=1)).isoformat(),
        }, True))

    login(client, "owner")
    rows = allocations()
    assert {row["status"] for row in rows} == {"QUOTED"}
    assert rpc(client, "charter.transition", {
        "id": rows[0]["id"], "version": rows[0]["version"], "action": "CONFIRM",
    }, True).status_code == 409
    result(rpc(client, "charter.transitionGroup", action(rows), True))
    assert {row["status"] for row in allocations()} == {"CONFIRMED"}
    assert len(result(rpc(client, "charter.live", {"bookingId": group}))) == 2
    login(client, "carrier")
    [carrier_booking] = allocations()
    [carrier_vehicle] = result(rpc(client, "charter.live", {"bookingId": carrier_booking["id"]}))
    assert carrier_vehicle["bookingId"] == carrier_booking["id"]
    login(client, "busdriver3")
    [assigned] = result(rpc(client, "charter.driverJobs"))
    login(client, "driver")
    [own] = [job for job in result(rpc(client, "charter.driverJobs")) if job["bookingId"] in {row["id"] for row in rows}]
    assert assigned["itinerary"]["outbound"]["assigned"]["passengers"] + own["itinerary"]["outbound"]["assigned"]["passengers"] == 23
    foreign = {"bookingId": assigned["bookingId"], "vehicleId": assigned["vehicle"]["id"], "action": "DECLINE"}
    assert rpc(client, "charter.respondJob", {**foreign, "reason": "Conflict"}, True).status_code == 404
    login(client, "busdriver3")
    assert rpc(client, "charter.respondJob", foreign, True).status_code == 400
    assert result(rpc(client, "charter.respondJob", {**foreign, "reason": "Conflict"}, True))["status"] == "DECLINED"
    login(client, "bus3")
    [supplier] = allocations()
    assert supplier["driverAssignments"][0]["reason"] == "Conflict"
    login(client, "owner")
    assert "gaps" in result(rpc(client, "charter.search", trip, True))[0]
    result(rpc(client, "charter.transitionGroup", action(allocations(), "CANCEL"), True))
    assert {row["status"] for row in allocations()} == {"CANCELLED"}
    login(client, "busdriver3")
    [cancelled] = result(rpc(client, "charter.driverJobs"))
    assert cancelled["status"] == "CANCELLED"
    login(client, "owner")
