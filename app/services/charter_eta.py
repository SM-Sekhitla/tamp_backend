"""Calculate charter ETAs outside GPS write transactions."""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.security import now
from app.db.models import MODELS
from app.db.session import AsyncSessionLocal
from app.schemas.domain import RouteInput
from app.services import maps
from app.services.notifications import notify_organisation
from app.services.rbac import ROLE_IDS

BOOKINGS = MODELS["charterBookings"]
FRESHNESS = timedelta(seconds=90)
REFRESH = timedelta(seconds=45)
DELAY = timedelta(minutes=5)
RECOVERY = timedelta(minutes=2)


def checkpoint(record, job, leg):
    from app.controllers.passenger_dispatch import leg_progress

    progress = leg_progress(job, leg)
    itinerary = record[leg]
    if progress["stage"] == "TO_PICKUP":
        return "Pickup", itinerary["origin"], itinerary["pickupAt"]
    if progress["stage"] in {"AT_PICKUP", "ON_ROUTE"}:
        index = progress["nextStop"]
        if index < len(itinerary["stops"]):
            return f"Stop {index + 1}", itinerary["stops"][index], None
        field = "returnArrivalBy" if leg == "returnLeg" else "outboundArrivalBy"
        return "Drop-off", itinerary["destination"], record["quote"].get(field)
    return None


def candidate(record, job, clock):
    from app.controllers.passenger_dispatch import active_leg, leg_progress

    leg = active_leg(job)
    position = job.get("position")
    if not leg or not position or position["leg"] != leg:
        return None
    if clock - datetime.fromisoformat(position["recordedAt"]) >= FRESHNESS:
        return None
    progress = leg_progress(job, leg)
    target = checkpoint(record, job, leg)
    if target is None:
        return None
    return leg, progress["revision"], position, target


def public_eta(record, job, clock):
    snapshot = candidate(record, job, clock)
    eta = job.get("eta")
    if not snapshot or not eta:
        return None
    leg, revision, _, _ = snapshot
    if (eta["leg"] != leg or eta["revision"] != revision or not eta["arrivalAt"]
            or clock - datetime.fromisoformat(eta["positionAt"]) >= FRESHNESS):
        return None
    return {key: eta[key] for key in ("checkpoint", "label", "arrivalAt", "deadline", "atRisk", "positionAt")}


def pending(db):
    clock = now()
    tasks = []
    confirmed = db.scalars(select(BOOKINGS).where(BOOKINGS.data["status"].as_string() == "CONFIRMED"))
    for row in confirmed:
        for vehicle_id, job in row.data.get("driverJobs", {}).items():
            snapshot = candidate(row.data, job, clock)
            if snapshot is None:
                continue
            leg, revision, position, target = snapshot
            eta = job.get("eta", {})
            if (eta.get("leg") == leg and eta.get("revision") == revision
                    and clock - datetime.fromisoformat(eta["checkedAt"]) < REFRESH):
                continue
            tasks.append((eta.get("checkedAt", ""), row.id, vehicle_id, leg, revision, position, target))
    return sorted(tasks, key=lambda task: task[:3])[:30]


def write_eta(db, task, route, clock=None):
    _, booking_id, vehicle_id, leg, revision, position, target = task
    clock = clock or now()
    row = db.get(BOOKINGS, booking_id, with_for_update=True)
    if not row or row.data["status"] != "CONFIRMED":
        return
    jobs = dict(row.data.get("driverJobs", {}))
    job = dict(jobs.get(vehicle_id, {}))
    current = candidate(row.data, job, clock)
    if (not current or current[:2] != (leg, revision)
            or clock - datetime.fromisoformat(position["recordedAt"]) >= FRESHNESS):
        return
    previous = job.get("eta", {})
    if (previous.get("leg") == leg and previous.get("revision") == revision
            and datetime.fromisoformat(previous["positionAt"]) >= datetime.fromisoformat(position["recordedAt"])):
        return

    kind, place, deadline = target
    arrival = datetime.fromisoformat(position["recordedAt"]) + timedelta(minutes=route["durationMin"]) if route else None
    due = datetime.fromisoformat(deadline) if deadline else None
    alerts = dict(job.get("delayAlerts", {}))
    key = f"{leg}:{kind}"
    already_alerted = alerts.get(key, False)
    at_risk = bool(arrival and due and arrival >= due + (RECOVERY if already_alerted else DELAY))
    if arrival and due:
        alerts[key] = at_risk
    job.update(eta={
        "leg": leg, "revision": revision, "checkpoint": kind, "label": place["label"],
        "arrivalAt": arrival.isoformat() if arrival else None,
        "deadline": deadline, "atRisk": at_risk, "checkedAt": clock.isoformat(),
        "positionAt": position["recordedAt"],
    }, delayAlerts=alerts)
    jobs[vehicle_id] = job
    row.data = {**row.data, "driverJobs": jobs}
    if at_risk and not already_alerted:
        vehicle = next(v for v in row.data["quote"]["vehicles"] if v["id"] == vehicle_id)
        minutes = int((arrival - due).total_seconds() / 60)
        direction = "Outbound" if leg == "outbound" else "Return"
        for org, roles in (
            (row.organisation_id, ("FREIGHT_OWNER", "CARGO_OPERATOR", "TRANSPORTER", "DISPATCHER")),
            (row.supplier_id, ("TRANSPORTER", "DISPATCHER")),
        ):
            notify_organisation(db, org, title=f"Charter {kind.lower()} delay expected",
                                detail=f'{vehicle["registration"]} · {direction} · about {minutes} min late · {place["label"]}',
                                subject_type="CHARTER_BOOKING", subject_id=row.id,
                                role_ids=[ROLE_IDS[role] for role in roles])


async def refresh_charter_etas():
    async with AsyncSessionLocal() as db:
        tasks = await db.run_sync(pending)
    concurrency = asyncio.Semaphore(6)

    async def refresh(task):
        _, _, _, _, _, position, (_, destination, _) = task
        async with concurrency:
            route = await maps.route(RouteInput(
                origin={key: position[key] for key in ("lat", "lng")},
                destination={key: destination[key] for key in ("lat", "lng")},
            ))
            road_route = route if route and route.get("provider") != "estimated" else None
            async with AsyncSessionLocal.begin() as db:
                await db.run_sync(write_eta, task, road_route)

    results = await asyncio.gather(*(refresh(task) for task in tasks), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logging.getLogger("tamp.charter_eta").error("Charter ETA refresh failed", exc_info=result)
