import math
from datetime import datetime

from app.core.config import settings

BODIES = {
    "GENERAL_PALLETISED": ["TAUTLINER", "FLATBED", "DROPSIDE"],
    "BULK_DRY": ["TIPPER", "SIDE_TIPPER"],
    "BULK_LIQUID": ["TANKER"],
    "REFRIGERATED": ["REFRIGERATED"],
    "ABNORMAL": ["LOWBED", "FLATBED"],
    "CONTAINERISED": ["FLATBED"],
    "LIVESTOCK": ["DROPSIDE"],
    "HAZARDOUS": ["TANKER", "TAUTLINER"],
}


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def score(load, truck, operator, owner):
    km = distance(load["origin"], truck["currentLocation"])
    start = max(dt(load["pickupWindow"]["from"]), dt(truck["availableFrom"]))
    end = min(dt(load["pickupWindow"]["to"]), dt(truck["availableTo"]))
    tests = [
        ("R-CAPACITY", "Payload capacity", truck["payloadCapacityKg"] >= load["weightKg"]),
        ("R-BODY", "Compatible equipment", truck["bodyType"] in BODIES[load["cargoType"]]),
        ("R-WINDOW", "Availability overlaps pickup", end >= start),
        ("R-RADIUS", "Within 150 km", km <= 150),
        ("R-STATUS", "Truck available", truck["status"] == "AVAILABLE"),
        (
            "R-ACTIVE",
            "Active verified parties",
            not operator.get("suspended")
            and not owner.get("suspended")
            and operator.get("verification") == "VERIFIED"
            and owner.get("verification") == "VERIFIED",
        ),
    ]
    utilisation = load["weightKg"] / truck["payloadCapacityKg"]
    lane = any(
        item["origin"] == load["origin"]["label"]
        and item["destination"] == load["destination"]["label"]
        for item in truck.get("preferredLanes", [])
    )
    values = [
        (
            "R-PROXIMITY",
            "Truck is near pickup",
            30,
            max(0, 30 * (1 - km / 150)),
            f"{km} km from pickup",
        ),
        (
            "R-CAPACITY-FIT",
            "Capacity suits load",
            25,
            25 * min(1, utilisation / 0.8) * (0.9 if utilisation >= 1 else 1),
            f"{utilisation:.0%} utilisation",
        ),
        (
            "R-TIMING",
            "Pickup timing",
            20,
            20 * max(0, min(1, (end - start).total_seconds() / 21600)),
            "Pickup overlap, up to 6 hours",
        ),
        (
            "R-LANE",
            "Preferred lane",
            15,
            15
            if lane
            else 7.5
            if truck["currentLocation"]["province"] == load["destination"]["province"]
            else 0,
            "Lane or destination province fit",
        ),
        (
            "R-RATING",
            "Operator rating",
            10,
            2 * operator["ratingAvg"] if operator.get("ratingAvg") is not None else 5,
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
