from datetime import timedelta

from app.core.security import now
from app.services.matching import score

from .conftest import login, result, rpc
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
