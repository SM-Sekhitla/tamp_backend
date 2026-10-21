from .conftest import login, result
from .test_workflow import fixtures, sync


def test_truck_registration_creates_permanent_vehicle_and_assignment(client, accounts):
    _, truck, _ = fixtures(accounts)
    login(client, "carrier")

    snapshot = result(sync(client, trucks=[truck]))["snapshot"]

    saved = next(item for item in snapshot["trucks"] if item["id"] == truck["id"])
    vehicle = next(item for item in snapshot["vehicles"] if item["id"] == saved["vehicleId"])
    assignment = next(
        item
        for item in snapshot["vehicleDriverAssignments"]
        if item["vehicleId"] == vehicle["id"] and item["status"] == "ACTIVE"
    )
    assert vehicle["registration"] == truck["registration"]
    assert vehicle["ownerId"] == accounts["carrier"]["id"]
    assert assignment["driverId"] == accounts["driver"]["id"]
