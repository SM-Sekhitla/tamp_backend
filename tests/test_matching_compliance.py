from app.services.matching import score


def _record(vehicle_verification):
    load = {
        "cargoType": "GENERAL_PALLETISED",
        "weightKg": 10_000,
        "origin": {"label": "Pretoria", "lat": -25.7479, "lng": 28.2293},
        "destination": {"label": "Johannesburg", "lat": -26.2041, "lng": 28.0473},
        "pickupWindow": {"from": "2030-01-01T08:00:00+00:00", "to": "2030-01-01T14:00:00+00:00"},
    }
    truck = {
        "payloadCapacityKg": 12_000,
        "bodyType": "TAUTLINER",
        "currentLocation": load["origin"],
        "availableFrom": "2030-01-01T06:00:00+00:00",
        "availableTo": "2030-01-01T18:00:00+00:00",
        "status": "AVAILABLE",
        "licenceNumber": "LIC-1",
        "licenceExpiry": "2031-01-01",
        "documents": ["licence", "roadworthy", "insurance"],
        "vehicleVerification": vehicle_verification,
        "preferredLanes": [],
    }
    party = {"verification": "VERIFIED", "suspended": False, "ratingAvg": 5, "ratingCount": 1}
    return score(load, truck, party, party)


def test_pending_vehicle_is_excluded_from_matching():
    pending = _record("PENDING")
    assert pending["passed"] is False
    assert next(item for item in pending["hardFilterResults"] if item["ruleId"] == "R-VEHICLE-REVIEW")["passed"] is False

    approved = _record("VERIFIED")
    assert next(item for item in approved["hardFilterResults"] if item["ruleId"] == "R-VEHICLE-REVIEW")["passed"] is True

