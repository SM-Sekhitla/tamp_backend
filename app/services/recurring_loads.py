"""Materialise recurring load templates into ordinary marketplace loads."""

from copy import deepcopy
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.security import iso, new_id, now
from app.services.domain import lock_domain, refresh_suggestions
from app.services.matching import dt
from app.services.records import put, rows

JOHANNESBURG = ZoneInfo("Africa/Johannesburg")


def _next_post(after_date, days):
    for offset in range(1, 8):
        candidate = after_date + timedelta(days=offset)
        if candidate.weekday() in days:
            return datetime.combine(candidate, time(0, 5), JOHANNESBURG)
    return datetime.combine(after_date + timedelta(days=7), time(0, 5), JOHANNESBURG)


def _shift(value, delta):
    return (dt(value) + delta).isoformat()


def materialize_recurring_loads(db):
    """Create every due occurrence exactly once while holding the domain lock."""
    lock_domain(db)
    current = now()
    created = 0
    for row in list(rows(db, "loads")):
        template = row.data
        recurrence = template.get("recurrence")
        if not recurrence or not recurrence.get("isTemplate") or not recurrence.get("active"):
            continue
        original_next_post = dt(recurrence["nextPostAt"])
        next_post = original_next_post
        ends_on = dt(recurrence["endsOn"]) if recurrence.get("endsOn") else None
        while next_post <= current and (ends_on is None or next_post <= ends_on):
            occurrence_date = next_post.astimezone(JOHANNESBURG).date()
            template_pickup = dt(template["pickupWindow"]["from"])
            local_pickup = template_pickup.astimezone(JOHANNESBURG)
            occurrence_pickup = local_pickup.replace(
                year=occurrence_date.year,
                month=occurrence_date.month,
                day=occurrence_date.day,
            )
            delta = occurrence_pickup - template_pickup
            occurrence = deepcopy(template)
            occurrence_id = new_id("LD")
            occurrence.update(
                id=occurrence_id,
                reference=occurrence_id,
                status="POSTED",
                createdAt=iso(),
                pickupWindow={
                    "from": _shift(template["pickupWindow"]["from"], delta),
                    "to": _shift(template["pickupWindow"]["to"], delta),
                },
                deliveryBy=_shift(template["deliveryBy"], delta),
            )
            occurrence["stops"] = [
                {
                    **stop,
                    "id": new_id("LS"),
                    "status": "PENDING",
                    "window": {
                        "from": _shift(stop["window"]["from"], delta),
                        "to": _shift(stop["window"]["to"], delta),
                    },
                }
                for stop in template.get("stops", [])
            ]
            occurrence["recurrence"] = {
                **recurrence,
                "active": False,
                "isTemplate": False,
                "occurrenceDate": occurrence_pickup.isoformat(),
                "nextPostAt": occurrence_pickup.isoformat(),
            }
            put(db, "loads", occurrence, row.owner_id)
            created += 1
            next_post = _next_post(occurrence_date, recurrence["daysOfWeek"])
        updated_recurrence = {**recurrence, "nextPostAt": next_post.isoformat()}
        if ends_on is not None and next_post > ends_on:
            updated_recurrence["active"] = False
        if next_post != original_next_post or updated_recurrence["active"] != recurrence.get("active"):
            put(db, "loads", {**template, "recurrence": updated_recurrence}, row.owner_id)
    if created:
        refresh_suggestions(db)
    return created
