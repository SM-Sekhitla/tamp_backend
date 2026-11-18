"""Expiry and release rules for abandoned marketplace engagements."""

from app.core.security import iso, now
from app.services.matching import dt
from app.services.records import needed, put, rows


def expire_stale_engagements(db) -> int:
    expired = 0
    current_time = now()
    for row in rows(db, "matches"):
        match = row.data
        if match.get("status") not in {"SUGGESTED", "OFFERED", "ACCEPTED"}:
            continue
        if dt(match["expiresAt"]) > current_time:
            continue
        put(db, "matches", {**match, "status": "EXPIRED", "expiredAt": iso()})
        expired += 1
        if match["status"] == "ACCEPTED":
            load = needed(db, "loads", match["loadId"], True).data
            truck = needed(db, "trucks", match["truckPostingId"], True).data
            if load.get("status") == "MATCHED":
                put(db, "loads", {**load, "status": "POSTED"})
            if truck.get("status") == "RESERVED":
                put(db, "trucks", {**truck, "status": "AVAILABLE"})

    for row in rows(db, "match_offers"):
        offer = row.data
        if offer.get("status") in {"OPEN", "PARTIALLY_ACCEPTED"} and dt(offer["expiresAt"]) <= current_time:
            put(db, "match_offers", {**offer, "status": "EXPIRED", "expiredAt": iso()})
    return expired
