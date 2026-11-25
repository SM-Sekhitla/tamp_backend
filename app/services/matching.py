import math
from datetime import datetime, timezone

from app.core.config import settings
from app.db.models import PlatformSetting

DEFAULT_MATCHING_CONFIG = {
    "maxPickupRadiusKm": 150,
    "minimumScore": 70,
    "weights": {"proximity": 30, "capacity": 25, "timing": 20, "lane": 15, "rating": 10},
    "requireValidLicence": True,
    "requireDocuments": True,
    "requireVerifiedParties": True,
}


def matching_config(db):
    record = db.get(PlatformSetting, "matching")
    if record is None:
        return {**DEFAULT_MATCHING_CONFIG, "weights": dict(DEFAULT_MATCHING_CONFIG["weights"])}
    value = record.value or {}
    return {**DEFAULT_MATCHING_CONFIG, **value, "weights": {**DEFAULT_MATCHING_CONFIG["weights"], **value.get("weights", {})}}

BODIES = {
    "GENERAL_PALLETISED": ["TAUTLINER", "FLATBED", "DROPSIDE", "BOX_TRUCK", "CRANE_TRUCK"],
    "BULK_DRY": ["TIPPER", "SIDE_TIPPER"],
    "BULK_LIQUID": ["TANKER"],
    "REFRIGERATED": ["REFRIGERATED"],
    "ABNORMAL": ["LOWBED", "FLATBED"],
    "CONTAINERISED": ["FLATBED", "CONTAINER_SKELETAL"],
    "LIVESTOCK": ["LIVESTOCK_CARRIER", "DROPSIDE"],
    "VEHICLES": ["CAR_CARRIER"],
    "HAZARDOUS": ["TANKER", "TAUTLINER"],
}


def dt(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Legacy licence fields may contain YYYY-MM-DD rather than an ISO timestamp.
    # Normalise them before comparing with timezone-aware pickup windows.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def distance(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dl = math.radians(b["lng"] - a["lng"])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dl / 2) ** 2
    return round(6371 * 2 * math.asin(min(1, math.sqrt(h))) * 1.25, 1)


def quote(d):
    # Transparent baseline quote, not a negotiated tariff or live market price.
    per_km = (
        settings.quote_per_km_zar + min(d["weightKg"] / 1000, 40) * settings.quote_per_tonne_km_zar
    )
    multiplier = {"REFRIGERATED": 1.25, "HAZARDOUS": 1.4, "ABNORMAL": 1.5}.get(d["cargoType"], 1)
    line = round(d["distanceKm"] * per_km * multiplier, 2)
    return {
        "amount": round(settings.quote_base_zar + line, 2),
        "perKm": round(per_km * multiplier, 2),
        "distanceKm": d["distanceKm"],
        "components": [
            {"label": "Dispatch base", "amount": settings.quote_base_zar},
            {"label": "Distance, weight and cargo", "amount": line},
        ],
    }


def money(d):
    return {"amount": quote(d)["amount"], "currency": "ZAR", "vat": "incl"}


def _normalise_place(value):
    return " ".join(str(value or "").casefold().replace(",", " ").split())


def _lane_place_matches(saved_label, actual_place, saved_place=None):
    """Match a saved city/area against a more specific street address."""
    saved = _normalise_place(saved_label)
    actual = _normalise_place(actual_place.get("label"))
    if saved and actual and (saved == actual or saved in actual or actual in saved):
        return True
    if saved_place:
        saved_province = _normalise_place(saved_place.get("province"))
        actual_province = _normalise_place(actual_place.get("province"))
        if saved_province and saved_province == actual_province:
            # A selected preferred point represents its surrounding operating
            # area, not only an exact street string.
            return distance(saved_place, actual_place) <= 50
    return False


def _capacity_score(utilisation):
    """Reward useful capacity without a discontinuity at a full truck."""
    if utilisation <= 0.8:
        return 25 * max(0, utilisation) / 0.8
    if utilisation <= 0.92:
        return 25
    # Retain a small safety-headroom preference, declining smoothly to 22.5.
    return max(22.5, 25 - 2.5 * ((min(utilisation, 1) - 0.92) / 0.08))


def score(load, truck, operator, owner, config=None):
    config = config or DEFAULT_MATCHING_CONFIG
    radius = float(config["maxPickupRadiusKm"])
    weights = config["weights"]
    km = distance(load["origin"], truck["currentLocation"])
    start = max(dt(load["pickupWindow"]["from"]), dt(truck["availableFrom"]))
    end = min(dt(load["pickupWindow"]["to"]), dt(truck["availableTo"]))
    licence_expiry = truck.get("licenceExpiry")
    licence_valid = (
        bool(truck.get("licenceNumber"))
        and bool(licence_expiry)
        and dt(licence_expiry) >= start
    )
    documents_complete = len(truck.get("documents", [])) >= 3
    # Legacy records without the field remain eligible until they are next
    # edited/migrated. Every newly synced vehicle carries an explicit review
    # state and must be approved before matching.
    vehicle_verified = truck.get("vehicleVerification") in (None, "VERIFIED")
    tests = [
        ("R-CAPACITY", "Payload capacity", truck["payloadCapacityKg"] >= load["weightKg"]),
        ("R-BODY", "Compatible equipment", truck["bodyType"] in BODIES[load["cargoType"]]),
        ("R-WINDOW", "Availability overlaps pickup", end >= start),
        ("R-RADIUS", f"Within {radius:g} km", km <= radius),
        ("R-STATUS", "Truck available", truck["status"] == "AVAILABLE"),
        ("R-LICENCE", "Vehicle licence number and expiry valid through pickup", licence_valid or not config["requireValidLicence"]),
        ("R-DOCUMENTS", "Required vehicle documents uploaded", documents_complete or not config["requireDocuments"]),
        ("R-VEHICLE-REVIEW", "Vehicle compliance review approved", vehicle_verified),
        (
            "R-ACTIVE",
            "Active verified parties",
            not config["requireVerifiedParties"] or (not operator.get("suspended")
            and not owner.get("suspended")
            and operator.get("verification") == "VERIFIED"
            and owner.get("verification") == "VERIFIED"),
        ),
    ]
    utilisation = load["weightKg"] / truck["payloadCapacityKg"]
    preferred_lanes = truck.get("preferredLanes", [])
    lane = any(
        _lane_place_matches(item["origin"], load["origin"], item.get("originPlace"))
        and _lane_place_matches(
            item["destination"], load["destination"], item.get("destinationPlace")
        )
        for item in preferred_lanes
    )
    destination_province_fit = any(
        _normalise_place((item.get("destinationPlace") or {}).get("province"))
        == _normalise_place(load["destination"].get("province"))
        for item in preferred_lanes
        if (item.get("destinationPlace") or {}).get("province")
    )
    overlap_hours = max(0, (end - start).total_seconds() / 3600)
    values = [
        (
            "R-PROXIMITY",
            "Truck is near pickup",
            weights["proximity"],
            max(0, weights["proximity"] * (1 - km / radius)),
            f"{km} km from pickup",
        ),
        (
            "R-CAPACITY-FIT",
            "Capacity suits load",
            weights["capacity"],
            _capacity_score(utilisation) * weights["capacity"] / 25,
            f"{utilisation:.0%} utilisation; 80–92% is the preferred operating range",
        ),
        (
            "R-TIMING",
            "Pickup timing",
            weights["timing"],
            weights["timing"] * max(0, min(1, overlap_hours / 6)),
            f"{overlap_hours:.1f} hours of pickup-window overlap; full score at 6 hours",
        ),
        (
            "R-LANE",
            "Preferred lane",
            weights["lane"],
            weights["lane"]
            if lane
            else weights["lane"] / 2
            if destination_province_fit
            else 0,
            "Saved lane or operating area" if lane else "Destination province fit" if destination_province_fit else "No preferred-lane fit",
        ),
        (
            "R-RATING",
            "Operator rating",
            weights["rating"],
            weights["rating"] * operator["ratingAvg"] / 5 if operator.get("ratingAvg") is not None else weights["rating"] / 2,
            "New to TAMP — no rating yet"
            if operator.get("ratingAvg") is None
            else str(operator["ratingAvg"]),
        ),
    ]
    breakdown = [
        {"ruleId": k, "label": label, "weight": w, "earned": round(v, 2), "detail": detail}
        for k, label, w, v, detail in values
    ]
    return {
        "truck": truck,
        "operator": operator,
        "score": round(sum(b["earned"] for b in breakdown), 2),
        "breakdown": breakdown,
        "hardFilterResults": [
            {
                "ruleId": k,
                "label": label,
                "passed": passed,
                "detail": label + (" passed" if passed else " failed"),
            }
            for k, label, passed in tests
        ],
        "passed": all(p for _, _, p in tests),
    }


def fleet_recommendations(scored, trucks_required, minimum_score=0):
    """Rank eligible inventory by carrier and build a minimum-carrier plan.

    A carrier that can cover the remaining requirement is preferred. Otherwise
    the carrier contributing the most eligible trucks is selected, followed by
    the next largest fleet until the requirement is covered. Match quality and
    carrier reputation break capacity ties; each carrier's best-scoring trucks
    are allocated first.
    """
    required = max(1, int(trucks_required or 1))
    groups = {}
    for result in scored:
        if not result["passed"] or result["score"] < minimum_score:
            continue
        operator = result["operator"]
        groups.setdefault(operator["id"], {"operator": operator, "results": []})[
            "results"
        ].append(result)
    ranked_groups = []
    for group in groups.values():
        group["results"].sort(key=lambda item: -item["score"])
        group["eligibleCount"] = len(group["results"])
        group["averageScore"] = round(
            sum(item["score"] for item in group["results"]) / len(group["results"]), 2
        )
        group["companyRating"] = group["operator"].get("ratingAvg")
        ranked_groups.append(group)
    ranked_groups.sort(
        key=lambda group: (
            -group["eligibleCount"],
            -group["averageScore"],
            -(group["companyRating"] or 0),
            group["operator"].get("companyName", ""),
        )
    )
    remaining = required
    metadata = {}
    plan_rank = 0
    for group in ranked_groups:
        allocation = min(remaining, group["eligibleCount"]) if remaining > 0 else 0
        if allocation:
            plan_rank += 1
            remaining -= allocation
        for index, result in enumerate(group["results"]):
            metadata[result["truck"]["id"]] = {
                "fleetPlanRank": plan_rank if allocation else None,
                "operatorEligibleCount": group["eligibleCount"],
                "operatorAverageScore": group["averageScore"],
                "recommendedAllocation": allocation,
                "selectedForFleetPlan": index < allocation,
                "operatorCoveragePct": round(
                    min(100, group["eligibleCount"] / required * 100), 1
                ),
                "companyRating": group["companyRating"],
            }
    return {"required": required, "covered": required - remaining, "metadata": metadata}
