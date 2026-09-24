from datetime import timedelta

from app.core.security import now

from .conftest import login, result, rpc

PLACE = {"label": "Johannesburg", "province": "GP", "lat": -26.2041, "lng": 28.0473}
DEST = {"label": "Pretoria", "province": "GP", "lat": -25.7479, "lng": 28.2293}


def fixtures(accounts):
    start = (now() + timedelta(days=1)).isoformat()
    end = (now() + timedelta(days=2)).isoformat()
    load = dict(
        id="LD-test",
        reference="LD-test",
        ownerId=accounts["owner"]["id"],
        cargoType="GENERAL_PALLETISED",
        weightKg=10000,
        origin=PLACE,
        destination=DEST,
        distanceKm=70,
        pickupWindow={"from": start, "to": end},
        deliveryBy=end,
        status="POSTED",
        createdAt=now().isoformat(),
        requiredBodyTypes=["TAUTLINER"],
    )
    truck = dict(
        id="TR-test",
        transporterId=accounts["carrier"]["id"],
        driverId=accounts["driver"]["id"],
        registration="ABC123GP",
        bodyType="TAUTLINER",
        payloadCapacityKg=12000,
        currentLocation=PLACE,
        availableFrom=start,
        availableTo=end,
        status="AVAILABLE",
        createdAt=now().isoformat(),
    )
    match = dict(
        id="MT-test",
        loadId=load["id"],
        truckPostingId=truck["id"],
        score=99,
        breakdown=[],
        hardFilterResults=[],
        status="SUGGESTED",
        initiatedBy="SYSTEM",
        expiresAt=end,
        createdAt=now().isoformat(),
    )
    return load, truck, match


def sync(client, **changes):
    return rpc(client, "snapshot.sync", {"upserts": changes, "deleteIds": {}}, True)


def setup_match(client, accounts):
    load, truck, match = fixtures(accounts)
    login(client, "carrier")
    result(sync(client, trucks=[truck]))
    login(client, "owner")
    result(sync(client, loads=[load], matches=[match]))
    return load, truck, match


def test_complete_freight_lifecycle(client, accounts):
    load, truck, match = setup_match(client, accounts)
    result(
        rpc(client, "commands.acceptMatch", {"loadId": load["id"], "truckId": truck["id"]}, True)
    )
    assert rpc(client, "commands.confirmMatch", {"loadId": load["id"]}, True).status_code == 403
    login(client, "carrier")
    trip_id = result(rpc(client, "commands.confirmMatch", {"loadId": load["id"]}, True))["tripId"]
    assert rpc(client, "commands.confirmMatch", {"loadId": load["id"]}, True).status_code == 409
    login(client, "driver")
    for status in ["AT_PICKUP", "LOADED", "IN_TRANSIT", "AT_DROPOFF"]:
        assert (
            result(rpc(client, "commands.advanceTrip", {"loadId": load["id"]}, True))["status"]
            == status
        )
    assert rpc(client, "commands.advanceTrip", {"loadId": load["id"]}, True).status_code == 400
    assert (
        result(
            rpc(
                client,
                "commands.advanceTrip",
                {"loadId": load["id"], "proof": {"recipientName": "Recipient"}},
                True,
            )
        )["status"]
        == "DELIVERED"
    )
    result(rpc(client, "commands.advanceTrip", {"loadId": load["id"]}, True))
    login(client, "owner")
    snap = result(rpc(client, "snapshot.load"))
    assert snap["loads"][0]["status"] == "COMPLETED"
    assert snap["trips"][0]["id"] == trip_id
    assert len(snap["acceptances"]) == 2
    result(
        sync(
            client,
            ratings=[
                dict(
                    id="RT-test",
                    tripId=trip_id,
                    raterId=accounts["owner"]["id"],
                    rateeId=accounts["carrier"]["id"],
                    stars=5,
                    tags=[],
                    createdAt=now().isoformat(),
                )
            ],
        )
    )
    login(client, "carrier")
    assert result(rpc(client, "notifications.unreadCount")) > 0
    result(rpc(client, "notifications.markRead", {}, True))
    assert result(rpc(client, "notifications.unreadCount")) == 0


def test_ownership_server_fields_and_atomicity(client, accounts):
    load, truck, match = setup_match(client, accounts)
    login(client, "other")
    assert sync(client, loads=[{**load, "weightKg": 999}]).status_code == 403
    assert (
        sync(client, trucks=[{**truck, "transporterId": accounts["other"]["id"]}]).status_code
        == 403
    )
    assert (
        rpc(
            client, "commands.acceptMatch", {"loadId": load["id"], "truckId": truck["id"]}, True
        ).status_code
        == 403
    )
    assert not result(rpc(client, "snapshot.load"))["loads"]
    login(client, "owner")
    assert sync(client, matches=[{**match, "status": "CONFIRMED"}]).status_code == 409
    fresh = {**load, "id": "LD-rollback", "reference": "LD-rollback"}
    assert (
        sync(
            client,
            loads=[fresh],
            matches=[{**match, "id": "MT-forged", "loadId": "LD-rollback", "status": "CONFIRMED"}],
        ).status_code
        == 403
    )
    assert not any(
        item["id"] == "LD-rollback" for item in result(rpc(client, "snapshot.load"))["loads"]
    )
    carrier = next(
        p
        for p in result(rpc(client, "snapshot.load"))["parties"]
        if p["id"] == accounts["carrier"]["id"]
    )
    assert carrier["email"] == "" and carrier["phone"] == ""


def test_tracking_is_signed_and_private(client, accounts):
    setup_match(client, accounts)
    token = result(rpc(client, "tracking.link", {"loadId": "LD-test"}))["token"]
    result(rpc(client, "auth.logout", {}, True))
    view = result(rpc(client, "tracking.view", {"token": token}))
    assert "targetRate" not in view["loads"][0]
    assert view["parties"] == []
    assert rpc(client, "tracking.view", {"token": "LD-test"}).status_code == 404
    assert rpc(client, "tracking.view", {"token": token + "bad"}).status_code == 404


def test_generated_suggestions_and_snapshot_roundtrip(client, accounts):
    load, truck, _ = fixtures(accounts)
    login(client, "carrier")
    result(sync(client, trucks=[truck]))
    login(client, "owner")
    saved = result(sync(client, loads=[load]))["snapshot"]
    assert len(saved["matches"]) == 1
    assert saved["matches"][0]["breakdown"]
    assert saved["loads"][0]["targetRate"]["amount"] > 0
    # An unrelated carrier may see an open load but cannot rewrite its rate.
    login(client, "carrier")
    exposed = result(rpc(client, "snapshot.load"))
    assert (
        sync(
            client,
            loads=[
                {
                    **exposed["loads"][0],
                    "targetRate": {"amount": 1, "currency": "ZAR", "vat": "incl"},
                }
            ],
        ).status_code
        == 403
    )
    # Re-saving normalized inventory is idempotent.
    result(sync(client, trucks=exposed["trucks"]))


def test_concurrent_acceptance_has_one_winner(client, accounts):
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from app.main import app

    load, truck, _ = setup_match(client, accounts)
    login(client, "carrier")
    result(sync(client, trucks=[{**truck, "id": "TR-second", "registration": "SECOND"}]))
    login(client, "owner")
    cookie = client.cookies["tamp_session"]

    def accept(truck_id):
        with TestClient(app, headers={"X-TAMP-Request": "1"}) as c:
            c.cookies.set("tamp_session", cookie)
            return rpc(
                c, "commands.acceptMatch", {"loadId": load["id"], "truckId": truck_id}, True
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(accept, [truck["id"], "TR-second"]))
    assert statuses.count(200) == 1
    assert all(code in [200, 404, 409] for code in statuses)
    snap = result(rpc(client, "snapshot.load"))
    assert sum(m["status"] == "ACCEPTED" for m in snap["matches"]) == 1
