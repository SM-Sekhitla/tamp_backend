import re

from app.core.errors import require
from app.core.security import iso, new_id
from app.db.models import Party
from app.services import maps, matching
from app.services.domain import audit, lock_domain, sanitize_load, snapshot
from app.services.email import issue_code
from app.services.records import put, rows


def handle(procedure, data, db, request, response, party):
    d = data.model_dump(mode="json", by_alias=True, exclude_none=True)
    if procedure == "email.sendCode":
        return issue_code(db, "register:" + str(data.email).lower(), str(data.email).lower())
    if procedure == "phone.check":
        raw = re.sub(r"[\s()-]", "", data.raw)
        if raw.startswith("0"):
            raw = "+27" + raw[1:]
        valid = bool(re.fullmatch(r"\+27[6-8][0-9]{8}", raw))
        return (
            {"valid": valid, "e164": raw, "national": "0" + raw[3:]}
            if valid
            else {"valid": False, "reason": "Enter a valid South African mobile number"}
        )
    if procedure == "matching.bodyTypesForCargo":
        return matching.BODIES[data.cargoType]
    if procedure == "matching.roadDistanceKm":
        return matching.distance(d["a"], d["b"])
    if procedure.startswith("pricing."):
        return matching.money(d) if procedure.endswith("quoteMoney") else matching.quote(d)
    if procedure == "matching.score":
        # Preview scores never authorize a command. Use DB identities whenever available.
        from app.services.records import get

        load = get(db, "loads", data.load.id)
        ld = load.data if load else d["load"]
        require(load is not None or ld["ownerId"] == party.id)
        owner = db.get(Party, ld["ownerId"])
        require(owner is not None, "Load owner not found", 404)
        visible = {r["id"] for r in snapshot(db, party)["loads"]}
        require(load is None or load.id in visible)
        visible_trucks = {t["id"] for t in snapshot(db, party)["trucks"]}
        result = []
        for proposed in d["trucks"]:
            tr = get(db, "trucks", proposed["id"])
            require(tr is None or tr.id in visible_trucks)
            truck = tr.data if tr else proposed
            require(tr is not None or truck["transporterId"] == party.id)
            op = db.get(Party, truck["transporterId"])
            require(op is not None, "Carrier not found", 404)
            # Never expose private contact fields through score previews.
            public = {**op.data, "email": "", "phone": ""}
            public.pop("kycDocument", None)
            scored = matching.score(ld, truck, public, owner.data)
            scored["truck"] = {**truck, "registration": "•••" + truck["registration"][-3:]}
            scored["truck"].pop("documents", None)
            result.append(scored)
        return sorted(result, key=lambda r: -r["score"])
    if procedure == "loads.list":
        return snapshot(db, party)["loads"]
    if procedure == "loads.create":
        lock_domain(db)
        ident = new_id("LD")
        ld = sanitize_load(
            db,
            party,
            {**d, "id": ident, "reference": ident, "status": "POSTED", "createdAt": iso()},
        )
        put(db, "loads", ld, party.id)
        audit(db, party, "LOAD_POSTED", "LOAD", ident, "Load posted")
        from app.services.domain import refresh_suggestions

        refresh_suggestions(db)
        return {"id": ident}
    if procedure.startswith("notifications."):
        records = [r for r in rows(db, "notifications") if r.owner_id == party.id]
        if procedure.endswith("unreadCount"):
            return sum(r.data["unread"] for r in records)
        if procedure.endswith("list"):
            return sorted([r.data for r in records], key=lambda r: r["at"], reverse=True)[
                : data.limit
            ]
        count = 0
        for r in records:
            if r.data["unread"] and (data.ids is None or r.id in data.ids):
                r.data = {**r.data, "unread": False}
                count += 1
        return {"updated": count}
    if procedure == "geocode.autocomplete":
        return maps.autocomplete(data)
    if procedure == "geocode.details":
        return maps.details(data)
    if procedure == "routes.compute":
        return maps.route(data)
    raise AssertionError(procedure)
