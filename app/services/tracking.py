import base64
import json
import secrets

from app.api.deps import is_admin
from app.core.errors import require
from app.core.security import now, token_hash
from app.services.domain import snapshot
from app.services.records import needed, rows


def link(db, party, load_id):
    visible = snapshot(db, party)
    require(any(item["id"] == load_id for item in visible["loads"]), "Load not found", 404)
    load = needed(db, "loads", load_id).data
    associated = any(
        m["loadId"] == load_id and m["status"] == "CONFIRMED" for m in visible["matches"]
    )
    require(load["ownerId"] == party.id or is_admin(party) or associated)
    payload = (
        base64.urlsafe_b64encode(json.dumps([load_id, int(now().timestamp()) + 7 * 86400]).encode())
        .decode()
        .rstrip("=")
    )
    return {"token": payload + "." + token_hash("tracking:" + payload)}


def view(db, token):
    try:
        payload, signature = token.split(".")
        require(
            secrets.compare_digest(signature, token_hash("tracking:" + payload)),
            "Tracking link invalid",
            404,
        )
        load_id, expiry = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        require(expiry > now().timestamp(), "Tracking link expired", 404)
    except (ValueError, TypeError):
        from app.core.errors import AppError

        raise AppError("Tracking link invalid", 404)
    load = needed(db, "loads", load_id).data
    match = next(
        (
            r.data
            for r in rows(db, "matches")
            if r.data["loadId"] == load_id and r.data["status"] == "CONFIRMED"
        ),
        None,
    )
    trip = next(
        (r.data for r in rows(db, "trips") if match and r.data["matchId"] == match["id"]), None
    )
    # Deliberately excludes prices, parties, receipts, proofs, driver and contacts.
    return {
        "loads": [
            {k: v for k, v in load.items() if k not in ["targetRate", "specialRequirements"]}
        ],
        "matches": [
            {
                "id": match["id"],
                "loadId": load_id,
                "truckPostingId": match["truckPostingId"],
                "status": "CONFIRMED",
            }
        ]
        if match
        else [],
        "trips": [
            {
                **trip,
                "events": [
                    {k: v for k, v in e.items() if k in ["status", "at"]} for e in trip["events"]
                ],
            }
        ]
        if trip
        else [],
        "trucks": [],
        "parties": [],
    }
