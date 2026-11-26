"""Driver assignment handoff for confirmed passenger charters."""

from datetime import datetime, timedelta, timezone
from math import inf

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB

from app.core.errors import require
from app.core.security import iso, now
from app.db.models import MODELS, OrganisationMembership, User
from app.services.notifications import notify_organisation, notify_user
from app.services.rbac import ROLE_IDS, permission_codes, require_permission

BOOKINGS = MODELS["charterBookings"]


def allocate_seats(leg, vehicles, *, build_manifest=False):
    """Choose wheelchair configurations that leave the most regular seats."""

    wheelchair_count = leg["wheelchairPositions"]
    regular_count = leg["passengers"] - wheelchair_count
    losses = {0: (0, ())}
    for vehicle in vehicles:
        if not vehicle["wheelchairPositions"]:
            continue
        for positions, (lost, selected) in list(losses.items()):
            covered = min(wheelchair_count, positions + vehicle["wheelchairPositions"])
            cost = lost + vehicle["displacedSeats"]
            if cost < losses.get(covered, (inf, ()))[0]:
                losses[covered] = cost, (*selected, vehicle["id"])
    lost, selected = losses.get(wheelchair_count, (inf, ()))
    require(sum(vehicle["seats"] for vehicle in vehicles) - lost >= regular_count,
            "The selected vehicles cannot seat everyone on one of the legs", 409)
    if not build_manifest:
        return None

    plan = {}
    for vehicle in vehicles:
        accessible = vehicle["id"] in selected
        wheelchairs = min(wheelchair_count, vehicle["wheelchairPositions"]) if accessible else 0
        seats = vehicle["seats"] - (vehicle["displacedSeats"] if accessible else 0)
        regular = min(regular_count, seats)
        plan[vehicle["id"]] = {"passengers": regular + wheelchairs, "wheelchairPositions": wheelchairs}
        wheelchair_count -= wheelchairs
        regular_count -= regular
    return plan


def initialise_jobs(db, row, manifest):
    record = row.data
    jobs = {}
    for vehicle in record["quote"]["vehicles"]:
        ident = vehicle["id"]
        jobs[ident] = {
            "status": "OFFERED",
            "offeredAt": record["updatedAt"],
            "legs": {leg: counts[ident] for leg, counts in manifest.items()},
        }
        if vehicle.get("driverId"):
            notify_user(
            db, vehicle["driverId"], title="New passenger charter assignment",
            detail=f'{vehicle["registration"]} · {record["outbound"]["origin"]["label"]}',
            subject_type="CHARTER_JOB", subject_id=row.id, organisation_id=row.supplier_id,
        )
    row.data = {**record, "driverJobs": jobs}


def cancel_jobs(db, row):
    if not row.data.get("quote"):
        return
    for vehicle in row.data["quote"]["vehicles"]:
        notify_user(
            db, vehicle["driverId"], title="Passenger charter cancelled",
            detail=f'{vehicle["registration"]} · {row.data["outbound"]["origin"]["label"]}',
            subject_type="CHARTER_JOB", subject_id=row.id, organisation_id=row.supplier_id,
        )


def assignment_status(row, vehicle_id):
    job = row.data.get("driverJobs", {}).get(vehicle_id, {})
    status = "CANCELLED" if row.data["status"] == "CANCELLED" else job.get("status", "OFFERED")
    return {"status": status, **{key: job[key] for key in ("respondedAt", "reason", "legs") if key in job}}


def leg_progress(job, leg):
    return job.get("progress", {}).get(leg, {"stage": "SCHEDULED", "nextStop": 0, "revision": 0})


def public_progress(job, leg):
    progress = leg_progress(job, leg)
    return {**{key: value for key, value in progress.items() if key in {"stage", "nextStop", "revision", "updatedAt"}},
            "events": [{key: event[key] for key in ("action", "at", "stopIndex") if key in event}
                       for event in progress.get("events", [])]}


def vehicle_progress(row):
    if not was_confirmed(row):
        return []
    record = row.data
    return [{
        "vehicleId": v["id"],
        "registration": v["registration"],
        "status": assignment_status(row, v["id"])["status"],
        "legs": {key: public_progress(record.get("driverJobs", {}).get(v["id"], {}), key)
                 for key in ("outbound", "returnLeg") if record.get(key)},
    } for v in record["quote"]["vehicles"]]


def advance_leg(db, row, vehicle, party, data):
    require_permission(db, party, "trips.manage", row.supplier_id)
    require(row.data["status"] == "CONFIRMED", "This charter is no longer confirmed", 409)
    record = row.data
    leg = record.get(data.leg)
    require(leg, "This charter has no return leg", 404)
    jobs = dict(record.get("driverJobs", {}))
    job = jobs.get(vehicle["id"], {})
    require(job.get("status") == "ACCEPTED", "Accept the assignment before starting a leg", 409)
    current = leg_progress(job, data.leg)
    revision = current["revision"]
    if revision == data.revision + 1 and current.get("lastAction") == data.action:
        return driver_job(row, vehicle)
    require(revision == data.revision, "This leg changed. Review the current step.", 409)
    stage, stop = current["stage"], current["nextStop"]
    advance = {
        "SCHEDULED": ("START", "TO_PICKUP"),
        "TO_PICKUP": ("ARRIVE_PICKUP", "AT_PICKUP"),
        "AT_PICKUP": ("BOARD", "ON_ROUTE"),
    }.get(stage)
    if stage == "ON_ROUTE":
        advance = ("COMPLETE_STOP", "ON_ROUTE") if stop < len(leg["stops"]) else ("COMPLETE", "COMPLETED")
    require(advance and data.action == advance[0], "Complete the current trip step first", 409)
    if data.action == "START":
        require(data.leg != "returnLeg" or leg_progress(job, "outbound")["stage"] == "COMPLETED",
                "Complete the outbound leg before starting the return", 409)

    at = iso()
    progress = {
        **current, "stage": advance[1], "nextStop": stop + int(data.action == "COMPLETE_STOP"),
        "revision": revision + 1, "lastAction": data.action, "updatedAt": at,
        "events": [*current.get("events", []), {
            "action": data.action, "at": at, "byUserId": party.id,
            **({"stopIndex": stop} if data.action == "COMPLETE_STOP" else {}),
        }],
    }
    jobs[vehicle["id"]] = {**job, "progress": {**job.get("progress", {}), data.leg: progress}}
    if data.action == "COMPLETE":
        jobs[vehicle["id"]].pop("position", None)
    updated = {**record, "driverJobs": jobs, "updatedAt": at}
    if all(vehicle_complete(updated, v["id"]) for v in record["quote"]["vehicles"]):
        updated["completedAt"] = updated.get("completedAt") or at
        updated["invoice"] = updated.get("invoice") or {
            "number": f"CH-INV-{row.id}", "issuedAt": at, "status": "ISSUED",
            "total": record["quote"]["total"], "vat": record["quote"]["vat"], "currency": "ZAR",
            "customer": record["customer"], "supplier": record["supplier"],
        }
    row.data = updated
    if data.action == "COMPLETE":
        for org in (row.supplier_id, row.organisation_id):
            notify_organisation(db, org, title="Passenger charter leg completed",
                                detail=f'{vehicle.get("registration", vehicle["id"])} · {data.leg}',
                                subject_type="CHARTER_BOOKING", subject_id=row.id,
                                role_ids=[ROLE_IDS[r] for r in ("TRANSPORTER", "DISPATCHER", "FREIGHT_OWNER", "CARGO_OPERATOR")])
    return driver_job(row, vehicle)


def vehicle_complete(record, vehicle_id):
    job = record.get("driverJobs", {}).get(vehicle_id, {})
    return job.get("status") == "ACCEPTED" and all(
        leg_progress(job, key)["stage"] == "COMPLETED"
        for key in ("outbound", "returnLeg") if record.get(key)
    )


def active_leg(job):
    if job.get("status") != "ACCEPTED":
        return None
    return next((key for key in ("outbound", "returnLeg")
                 if leg_progress(job, key)["stage"] in {"TO_PICKUP", "AT_PICKUP", "ON_ROUTE"}), None)


def publish_position(db, row, vehicle, party, data):
    require_permission(db, party, "tracking.publish", row.supplier_id)
    require(row.data["status"] == "CONFIRMED", "This charter is no longer confirmed", 409)
    jobs = dict(row.data.get("driverJobs", {}))
    job = jobs.get(vehicle["id"], {})
    require(active_leg(job) == data.leg, "Location sharing requires an active charter leg", 409)
    clock = now()
    require(clock - timedelta(minutes=2) <= data.recordedAt <= clock + timedelta(seconds=30),
            "Send a recent GPS position", 400)
    started = leg_progress(job, data.leg)["events"][0]["at"]
    require(data.recordedAt >= datetime.fromisoformat(started) - timedelta(seconds=5),
            "Send a GPS position taken during this leg", 400)
    previous = job.get("position")
    if previous and previous["leg"] == data.leg:
        if (data.recordedAt <= datetime.fromisoformat(previous["recordedAt"])
                or clock - datetime.fromisoformat(previous["receivedAt"]) < timedelta(seconds=4)):
            return {"recordedAt": previous["recordedAt"]}
    position = data.model_dump(mode="json", include={"leg", "lat", "lng", "accuracyM"})
    position["recordedAt"] = data.recordedAt.astimezone(timezone.utc).isoformat()
    position["receivedAt"] = clock.isoformat()
    jobs[vehicle["id"]] = {**job, "position": position}
    # GPS updates do not change the commercial revision or history order.
    row.data = {**row.data, "driverJobs": jobs}
    return {"recordedAt": position["recordedAt"]}


def live_vehicles(row):
    from app.services.charter_eta import public_eta

    clock = now()
    jobs = row.data.get("driverJobs", {})
    vehicles = []
    for vehicle in vehicle_progress(row):
        job = jobs.get(vehicle["vehicleId"], {})
        leg = active_leg(job) if row.data["status"] == "CONFIRMED" else None
        position = job.get("position")
        visible_position = ({key: position[key] for key in ("lat", "lng", "accuracyM", "recordedAt")}
                            if leg and position and position["leg"] == leg else None)
        vehicles.append({
            **vehicle, "bookingId": row.id, "supplier": row.data["supplier"],
            "activeLeg": leg, "position": visible_position,
            "eta": public_eta(row.data, job, clock) if leg else None,
        })
    return vehicles


def supplier_assignments(db, row):
    quote = row.data.get("quote")
    if not quote or not was_confirmed(row):
        return []
    drivers = {user.id: user.data.get("contactName") for user in db.scalars(
        select(User).where(User.id.in_([v["driverId"] for v in quote["vehicles"]]))
    )}
    return [{
        "vehicleId": vehicle["id"], "registration": vehicle["registration"], "driverId": vehicle.get("driverId"),
        "canReassign": can_reassign(row, vehicle["id"]),
        "driverName": drivers.get(vehicle["driverId"]) or "Driver not assigned",
        **assignment_status(row, vehicle["id"]),
    } for vehicle in quote["vehicles"]]


def can_reassign(row, vehicle_id):
    if row.data["status"] != "CONFIRMED" or datetime.fromisoformat(row.data["quote"]["end"]) <= now():
        return False
    job = row.data.get("driverJobs", {}).get(vehicle_id, {})
    return leg_progress(job, "outbound")["stage"] == "SCHEDULED"


def reassign_driver(db, party, data, *, save=False):
    from app.controllers.passenger import SA, VEHICLES, drivers, publication_issue
    from app.controllers.passenger_charter import occupied_fleet, timestamp

    org = db.info.get("active_organisation_id")
    row = db.get(BOOKINGS, data.bookingId, with_for_update=save)
    require(org and row and row.supplier_id == org, "Charter allocation not found", 404)
    require_permission(db, party, "dispatch.manage", org)
    quote = row.data.get("quote") or {}
    quoted = next((v for v in quote.get("vehicles", []) if v["id"] == data.vehicleId), None)
    require(quoted and can_reassign(row, data.vehicleId),
            "Drivers can be replaced before the outbound leg starts", 409)
    vehicle = db.get(VEHICLES, data.vehicleId, with_for_update=save)
    require(vehicle and vehicle.organisation_id == org, "Charter vehicle not found", 404)
    start, end = timestamp(quote["start"]), timestamp(quote["end"])
    if save:
        require(quoted["driverId"] == data.expectedDriverId,
                "The assignment changed. Reopen the driver selector.", 409)
        db.scalar(select(User.id).where(User.id == data.driverId).with_for_update())
    _, busy = occupied_fleet(db, row.id, max(start, now()), end)
    busy.update(v["driverId"] for v in quote["vehicles"])
    through = end.astimezone(SA).date()
    candidates = {}
    for driver in drivers(db, org):
        issue = publication_issue(db, {**vehicle.data, "driverId": driver.id}, org,
                                  through=through, require_driver=True)
        if driver.id in busy:
            issue = "Driver already allocated during this reservation"
        elif "dispatch.respond" not in permission_codes(db, driver, org):
            issue = "Driver needs permission to accept assignments"
        elif "trips.manage" not in permission_codes(db, driver, org):
            issue = "Driver needs permission to operate trips"
        candidates[driver.id] = (driver, issue)
    eligible = {ident: driver for ident, (driver, issue) in candidates.items() if not issue}
    if not save:
        return {"expectedDriverId": quoted["driverId"], "drivers": sorted(
            [{"id": driver.id, "name": driver.data.get("contactName") or driver.email,
              "eligible": not issue, "reason": issue}
             for driver, issue in candidates.values()],
            key=lambda driver: (driver["name"].casefold(), driver["id"]),
        )}
    reason = data.reason.strip()
    require(reason, "Enter a reason for replacing the driver", 400)
    require(data.driverId in eligible, "This driver is unavailable, lacks access, or needs compliance approval", 409)
    record = row.data
    old = record.get("driverJobs", {}).get(vehicle.id, {})
    at = iso()
    audit = {
        "previousDriverId": quoted["driverId"], "driverId": data.driverId, "byUserId": party.id,
        "previousStatus": old.get("status", "OFFERED"), "previousReason": old.get("reason"),
        "reason": reason, "at": at,
    }
    job = {
        "status": "OFFERED", "offeredAt": at,
        "legs": old.get("legs", {}), "reassignments": [*old.get("reassignments", []), audit],
    }
    row.data = {
        **record,
        "quote": {**quote, "vehicles": [
            {**v, "driverId": data.driverId} if v["id"] == vehicle.id else v for v in quote["vehicles"]
        ]},
        "driverJobs": {**record.get("driverJobs", {}), vehicle.id: job},
        "updatedAt": at,
        "history": [*record["history"], {"status": "DRIVER_REASSIGNED", "at": at,
                                       "reason": f'Driver assignment updated for {quoted["registration"]}'}],
    }
    for driver_id, title in (
        (quoted["driverId"], "Passenger charter assignment withdrawn"),
        (data.driverId, "New passenger charter assignment"),
    ):
        notify_user(db, driver_id, title=title, detail=quoted["registration"],
                    subject_type="CHARTER_JOB", subject_id=row.id, organisation_id=org)
    notify_organisation(
        db, row.organisation_id, title="Passenger charter driver reassigned",
        detail=f'{quoted["registration"]} · Awaiting the replacement driver’s response',
        subject_type="CHARTER_BOOKING", subject_id=row.id,
        role_ids=[ROLE_IDS[role] for role in ("FREIGHT_OWNER", "CARGO_OPERATOR", "TRANSPORTER", "DISPATCHER")],
    )
    return {"vehicleId": vehicle.id, "status": "OFFERED"}


def was_confirmed(row):
    return row.data["status"] == "CONFIRMED" or (
        row.data["status"] == "CANCELLED"
        and any(event["status"] == "CONFIRMED" for event in row.data["history"])
    )


def active_driver_membership(db, driver_id, organisation_id):
    return db.scalar(select(OrganisationMembership.id).where(
        OrganisationMembership.user_id == driver_id,
        OrganisationMembership.organisation_id == organisation_id,
        OrganisationMembership.status == "ACTIVE",
    )) is not None


def response_deadline(row, vehicle_id):
    job = row.data.get("driverJobs", {}).get(vehicle_id, {})
    deadline = row.data["quote"]["end"] if job.get("reassignments") else row.data["outbound"]["pickupAt"]
    return datetime.fromisoformat(deadline)


def driver_job(row, vehicle):
    record = row.data
    status = assignment_status(row, vehicle["id"])
    legs = status.pop("legs", {})
    itinerary = {}
    for key in ("outbound", "returnLeg"):
        if leg := record.get(key):
            itinerary[key] = {
                **{name: leg[name] for name in ("origin", "destination", "stops", "pickupAt")},
                "groupPassengers": leg["passengers"], "groupWheelchairPositions": leg["wheelchairPositions"],
                "assigned": legs.get(key),
                "progress": public_progress(record.get("driverJobs", {}).get(vehicle["id"], {}), key),
            }
    return {
        "bookingId": row.id, "supplier": record["supplier"], "customer": record["customer"],
        "reservationStartsAt": record["quote"]["start"],
        "responseDeadline": response_deadline(row, vehicle["id"]).isoformat(),
        "vehicle": {key: vehicle[key] for key in ("id", "registration", "make", "model")},
        "itinerary": itinerary, "notes": record["notes"],
        "rating": record.get("ratings", {}).get(vehicle["id"]),
        "completedAt": record.get("completedAt"), **status,
    }


def handle(action, data, db, party):
    require(party.data.get("role") == "DRIVER", "Driver access required", 403)
    if action == "driverJobs":
        org_ids = list(db.scalars(select(OrganisationMembership.organisation_id).where(
            OrganisationMembership.user_id == party.id, OrganisationMembership.status == "ACTIVE",
        )))
        permitted = [org for org in org_ids if "trips.read" in permission_codes(db, party, org)]
        quote = cast(BOOKINGS.data, JSONB)
        rows = db.scalars(select(BOOKINGS).where(
            BOOKINGS.supplier_id.in_(permitted),
            BOOKINGS.data["status"].as_string().in_(("CONFIRMED", "CANCELLED")),
            quote.contains({"quote": {"vehicles": [{"driverId": party.id}]}}),
        ))
        jobs = [
            driver_job(row, vehicle)
            for row in rows if was_confirmed(row)
            for vehicle in row.data["quote"]["vehicles"] if vehicle["driverId"] == party.id
        ]
        return sorted(jobs, key=lambda job: (job["itinerary"]["outbound"]["pickupAt"], job["bookingId"]))

    row = db.get(BOOKINGS, data.bookingId, with_for_update=True)
    vehicle = next((
        vehicle for vehicle in ((row.data.get("quote") or {}).get("vehicles", []) if row else [])
        if vehicle["id"] == data.vehicleId and vehicle["driverId"] == party.id
    ), None)
    require(row and vehicle and active_driver_membership(db, party.id, row.supplier_id),
            "Charter assignment not found", 404)
    if action == "publishPosition":
        return publish_position(db, row, vehicle, party, data)
    if action == "advanceJob":
        return advance_leg(db, row, vehicle, party, data)
    require_permission(db, party, "dispatch.respond", row.supplier_id)
    require(row.data["status"] == "CONFIRMED", "This charter is no longer confirmed", 409)
    status = "ACCEPTED" if data.action == "ACCEPT" else "DECLINED"
    current = assignment_status(row, vehicle["id"])["status"]
    if current == status:
        return driver_job(row, vehicle)
    require(current == "OFFERED" and response_deadline(row, vehicle["id"]) > now(),
            "This assignment is no longer awaiting a response", 409)
    reason = data.reason.strip()
    require(status != "DECLINED" or reason, "Enter a reason before declining the assignment", 400)
    jobs = dict(row.data.get("driverJobs", {}))
    jobs[vehicle["id"]] = {
        **jobs.get(vehicle["id"], {}),
        "status": status, "respondedAt": iso(), **({"reason": reason} if status == "DECLINED" else {}),
    }
    row.data = {**row.data, "driverJobs": jobs, "updatedAt": iso()}
    notify_organisation(
        db, row.supplier_id, title=f'Charter assignment {status.lower()}',
        detail=f'{vehicle["registration"]} · {party.data.get("contactName") or "Driver"}',
        subject_type="CHARTER_BOOKING", subject_id=row.id,
        role_ids=[ROLE_IDS["TRANSPORTER"], ROLE_IDS["DISPATCHER"]],
    )
    return driver_job(row, vehicle)
