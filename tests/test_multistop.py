from datetime import timedelta

from app.controllers.commands import next_trip_step
from app.core.security import now
from app.schemas.domain import LoadInput


def _place(label, lat, lng):
    return {"label": label, "province": "GP", "lat": lat, "lng": lng}


def test_multistop_load_validation_and_trip_sequence():
    start = now() + timedelta(days=1)
    stops = [
        {"id": "LS-1", "sequence": 0, "kind": "PICKUP", "place": _place("A", -26.2, 28.0), "window": {"from": start, "to": start + timedelta(hours=1)}},
        {"id": "LS-2", "sequence": 1, "kind": "PICKUP", "place": _place("B", -26.0, 28.2), "window": {"from": start + timedelta(hours=1), "to": start + timedelta(hours=2)}},
        {"id": "LS-3", "sequence": 2, "kind": "DROPOFF", "place": _place("C", -25.8, 28.4), "window": {"from": start + timedelta(hours=2), "to": start + timedelta(hours=3)}},
    ]
    value = LoadInput.model_validate({
        "ownerId": "USR-owner", "cargoType": "GENERAL_PALLETISED", "weightKg": 10000,
        "origin": stops[0]["place"], "destination": stops[-1]["place"], "distanceKm": 100,
        "pickupWindow": stops[0]["window"], "deliveryBy": stops[-1]["window"]["to"], "stops": stops,
    })
    assert len(value.stops) == 3
    assert next_trip_step({"status": "LOADED", "currentStopIndex": 0}, stops) == ("EN_ROUTE_PICKUP", 1)
    assert next_trip_step({"status": "LOADED", "currentStopIndex": 1}, stops) == ("IN_TRANSIT", 2)
    assert next_trip_step({"status": "DELIVERED", "currentStopIndex": 2}, stops) == ("COMPLETED", 2)
