from app.services.matching import fleet_recommendations, score


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


def test_city_preferred_lane_matches_a_specific_address_in_that_city():
    result = _record("VERIFIED")
    result["truck"]["preferredLanes"] = [
        {
            "origin": "Pretoria",
            "destination": "Johannesburg, South Africa",
        }
    ]
    rescored = score(
        {
            "cargoType": "GENERAL_PALLETISED",
            "weightKg": 10_000,
            "origin": {"label": "Pretoria", "lat": -25.7479, "lng": 28.2293},
            "destination": {
                "label": "44 Main Street, Johannesburg, South Africa",
                "lat": -26.2041,
                "lng": 28.0473,
            },
            "pickupWindow": {
                "from": "2030-01-01T08:00:00+00:00",
                "to": "2030-01-01T14:00:00+00:00",
            },
        },
        result["truck"],
        result["operator"],
        result["operator"],
    )
    lane = next(item for item in rescored["breakdown"] if item["ruleId"] == "R-LANE")
    assert lane["earned"] == lane["weight"]


def test_fleet_plan_prefers_largest_carrier_then_uses_fewest_companies():
    def candidate(carrier, truck, score_value, rating):
        return {
            "passed": True,
            "score": score_value,
            "truck": {"id": truck},
            "operator": {
                "id": carrier,
                "companyName": carrier,
                "ratingAvg": rating,
            },
        }

    scored = [
        *[candidate("large", f"L-{index}", 80 + index, 4.1) for index in range(7)],
        *[candidate("medium", f"M-{index}", 90 + index, 4.9) for index in range(4)],
        *[candidate("small", f"S-{index}", 95 + index, 5.0) for index in range(2)],
    ]
    plan = fleet_recommendations(scored, trucks_required=10, minimum_score=70)

    assert plan["covered"] == 10
    large = plan["metadata"]["L-0"]
    medium = plan["metadata"]["M-0"]
    small = plan["metadata"]["S-0"]
    assert large["fleetPlanRank"] == 1
    assert large["recommendedAllocation"] == 7
    assert medium["fleetPlanRank"] == 2
    assert medium["recommendedAllocation"] == 3
    assert small["fleetPlanRank"] is None
    assert sum(
        item["selectedForFleetPlan"] for item in plan["metadata"].values()
    ) == 10

