from datetime import timedelta
from io import BytesIO

from PIL import Image

from app.core.security import now
from app.services.matching import score
from app.services.records import needed

from .conftest import db_call, login, result, rpc
from .test_workflow import DEST, PLACE, fixtures


def test_fixed_rate_hire_and_linked_transport(client, accounts):
    start = now() + timedelta(days=2)
    end = start + timedelta(days=3)
    dimensions = {"lengthM": 10, "widthM": 3, "heightM": 3.5}
    asset_input = {
        "type": "ADT", "make": "Bell", "model": "B30E", "location": PLACE,
        "availability": {"start": start.isoformat(), "end": (end + timedelta(days=5)).isoformat()},
        "published": True, "ratedPayloadTonnes": 28, "transportWeightKg": 21500,
        "dimensions": dimensions, "rate": "8500.00", "unit": "DAY", "vat": "excl",
    }
    login(client, "carrier")
    asset = result(rpc(client, "assetHire.save", asset_input, True))
    booking_input = {
        "assetId": asset["id"], "listingVersion": asset["version"], "site": DEST,
        "start": start.isoformat(), "end": end.isoformat(),
    }
    login(client, "owner")
    assert result(rpc(client, "assetHire.list"))["assets"][0]["id"] == asset["id"]
    booking = result(rpc(client, "assetHire.request", booking_input, True))
    assert booking["total"] == "25500.00"
    login(client, "other")
    assert result(rpc(client, "assetHire.list"))["bookings"] == []
    other = result(rpc(client, "assetHire.request", booking_input, True))
    login(client, "carrier")
    result(rpc(client, "assetHire.save", {**asset_input, "id": asset["id"], "rate": "9000"}, True))
    row = result(rpc(client, "assetHire.transition", {"id": booking["id"], "action": "CONFIRM"}, True))
    assert row["total"] == "25500.00"
    image = BytesIO()
    Image.new("RGB", (16, 16), "yellow").save(image, "PNG")
    uploaded = client.post(
        "/api/uploads", data={"kind": "ASSET_HANDOVER_PHOTO"},
        files={"file": ("condition.png", image.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200
    photo = uploaded.json()["url"]
    report = {
        "id": booking["id"], "stage": "DELIVERY", "condition": "GOOD",
        "checks": {"assetIdentity": True, "conditionInspected": True, "attachmentsChecked": True},
        "photos": [photo],
    }
    assert result(rpc(client, "assetHire.handover", report, True))["photos"] == [photo]
    assert rpc(client, "assetHire.handover", report, True).status_code == 409
    assert rpc(client, "assetHire.handover", {**report, "stage": "RETURN"}, True).status_code == 409
    login(client, "other")
    assert client.get(photo).status_code == 403
    login(client, "owner")
    assert client.get(photo).status_code == 200
    assert rpc(client, "assetHire.handover", report, True).status_code == 403
    login(client, "carrier")
    conflict = rpc(client, "assetHire.transition", {"id": other["id"], "action": "CONFIRM"}, True)
    assert "unavailable" in conflict.text
    move_input = {
        "assetId": asset["id"], "bookingId": booking["id"], "origin": PLACE, "destination": DEST,
        "pickupWindow": {"from": (start - timedelta(hours=4)).isoformat(), "to": (start - timedelta(hours=2)).isoformat()},
        "deliveryBy": start.isoformat(),
    }
    login(client, "owner")
    move = result(rpc(client, "assetHire.move", move_input, True))
    assert result(rpc(client, "assetHire.move", move_input, True)) == move
    load = next(row for row in result(rpc(client, "loads.list")) if row["id"] == move["id"])
    assert load["weightKg"] == 21500
    assert load["transportDimensions"] == dimensions
    assert load["assetMoveId"] == asset["id"]
    assert result(rpc(client, "assetHire.list"))["moves"][0]["bookingId"] == booking["id"]
    login(client, "carrier")
    standalone = result(rpc(client, "assetHire.move", {**move_input, "bookingId": None}, True))
    assert standalone != move
    assert result(rpc(client, "assetHire.list"))["moves"][0]["id"] == standalone["id"]

    rental = {"id": booking["id"]}
    assert rpc(client, "assetHire.issueInvoice", rental, True).status_code == 409

    def move_start_to_past(db):
        record = needed(db, "assetBookings", booking["id"])
        record.data = {**record.data, "start": (now() - timedelta(hours=1)).isoformat()}

    assert rpc(client, "assetHire.transition", {**rental, "action": "START"}, True).status_code == 409

    login(client, "other")
    updated_asset = next(row for row in result(rpc(client, "assetHire.list"))["assets"] if row["id"] == asset["id"])
    next_rental = {"id": result(rpc(client, "assetHire.request", {
        **booking_input, "listingVersion": updated_asset["version"],
        "start": end.isoformat(), "end": (end + timedelta(days=1)).isoformat(),
    }, True))["id"]}
    login(client, "carrier")
    result(rpc(client, "assetHire.transition", {**next_rental, "action": "CONFIRM"}, True))

    db_call(move_start_to_past)
    missing_delivery = rpc(client, "assetHire.transition", {**rental, "action": "START"}, True)
    assert missing_delivery.status_code == 409
    assert "both record delivery" in missing_delivery.text

    login(client, "owner")
    renter_upload = client.post(
        "/api/uploads", data={"kind": "ASSET_HANDOVER_PHOTO"},
        files={"file": ("renter-condition.png", image.getvalue(), "image/png")},
    )
    assert renter_upload.status_code == 200
    renter_report = {**report, "photos": [renter_upload.json()["url"]]}
    result(rpc(client, "assetHire.handover", renter_report, True))

    login(client, "carrier")
    result(rpc(client, "assetHire.transition", {**rental, "action": "START"}, True))
    blocked = rpc(client, "assetHire.transition", {**next_rental, "action": "START"}, True)
    assert blocked.status_code == 409
    assert "still out on another rental" in blocked.text
    result(rpc(client, "assetHire.handover", {**report, "stage": "RETURN"}, True))
    missing_return = rpc(client, "assetHire.transition", {**rental, "action": "COMPLETE"}, True)
    assert missing_return.status_code == 409
    assert "both record return" in missing_return.text
    login(client, "owner")
    result(rpc(client, "assetHire.handover", {**renter_report, "stage": "RETURN"}, True))
    login(client, "carrier")
    result(rpc(client, "assetHire.transition", {**rental, "action": "COMPLETE"}, True))
    unblocked = rpc(client, "assetHire.transition", {**next_rental, "action": "START"}, True)
    assert "scheduled rental start time" in unblocked.text
    invoice = result(rpc(client, "assetHire.issueInvoice", rental, True))
    assert invoice["total"] == "29325.00"
    assert invoice["vat"] == "3825.00"
    assert result(rpc(client, "assetHire.issueInvoice", rental, True)) == invoice
    payment = {**rental, "amount": "10000.00", "reference": "EFT-001"}
    partial = result(rpc(client, "assetHire.recordPayment", payment, True))
    assert partial["status"] == "PARTIALLY_PAID"
    assert partial["balance"] == "19325.00"
    assert result(rpc(client, "assetHire.recordPayment", payment, True)) == partial
    assert rpc(client, "assetHire.recordPayment", {**payment, "amount": "1"}, True).status_code == 409
    assert rpc(client, "assetHire.recordPayment", {**payment, "reference": "EFT-002", "amount": "20000"}, True).status_code == 400
    settled = result(rpc(client, "assetHire.recordPayment", {**payment, "reference": "EFT-002", "amount": "19325"}, True))
    assert settled["status"] == "PAID"
    assert settled["balance"] == "0.00"
    login(client, "owner")
    assert rpc(client, "assetHire.recordPayment", payment, True).status_code == 403
    assert result(rpc(client, "assetHire.list"))["bookings"][0]["invoice"] == settled
    login(client, "other")
    assert {row["id"] for row in result(rpc(client, "assetHire.list"))["bookings"]} == {other["id"], next_rental["id"]}


def test_asset_move_dimensions_are_a_hard_filter(accounts):
    _, truck, *_ = fixtures(accounts)
    # Score against the exact same load before and after supplying clearance.
    load = {**fixtures(accounts)[0], "cargoType": "ABNORMAL", "weightKg": 10000,
            "transportDimensions": {"lengthM": 10, "widthM": 3, "heightM": 3}}
    truck["bodyType"] = "LOWBED"
    actor = {"verification": "VERIFIED", "ratingAvg": 5}
    for dimensions, expected in [(None, False), ({"lengthM": 10, "widthM": 3, "heightM": 3}, True)]:
        truck["transportDimensions"] = dimensions
        scored = score(load, truck, actor, actor)
        result = next(rule for rule in scored["hardFilterResults"] if rule["ruleId"] == "R-DIMENSIONS")
        assert result["passed"] is expected
