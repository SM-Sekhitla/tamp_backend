"""Whole-vehicle passenger charter request, quote and reservation lifecycle."""

from datetime import datetime, timedelta, timezone

from itertools import combinations, groupby, islice

from sqlalchemy import or_, select

from app.controllers import passenger_dispatch
from app.controllers.passenger import SA, VEHICLES, publication_issue, subtype_details
from app.core.errors import AppError, require
from app.core.security import iso, new_id, now
from app.db.models import MODELS, Organisation, User
from app.services.maps import verify_route_token
from app.services.notifications import notify_organisation
from app.services.rbac import ROLE_IDS, require_permission

BOOKINGS = MODELS["charterBookings"]


def timestamp(value):
    return datetime.fromisoformat(value)


def leg_terms(leg):
    proof = verify_route_token(leg.routeToken)

    def coordinates(point):
        return {"lat": point.lat, "lng": point.lng}

    require(
        proof["origin"] == coordinates(leg.origin)
        and proof["destination"] == coordinates(leg.destination)
        and proof["waypoints"] == [coordinates(stop) for stop in leg.stops],
        "Recalculate the route after changing the addresses", 409,
    )
    route = proof["route"]
    return {
        **leg.model_dump(mode="json", exclude={"routeToken"}),
        "pickupAt": leg.pickupAt.astimezone(timezone.utc).isoformat(),
        "estimatedArrival": (leg.pickupAt + timedelta(minutes=route["durationMin"])).astimezone(timezone.utc).isoformat(),
        "distanceKm": route["distanceKm"],
        "durationMin": route["durationMin"],
        "routeProvider": route["provider"],
    }


def confirmed_bookings():
    return select(BOOKINGS).where(BOOKINGS.data["status"].as_string() == "CONFIRMED")


def reservation_active(db, vehicle_id):
    return any(
        timestamp(row.data["quote"]["end"]) > now()
        and any(vehicle["id"] == vehicle_id for vehicle in row.data["quote"]["vehicles"])
        for row in db.scalars(confirmed_bookings())
    )


def validate_window(record, start, end):
    outbound, inbound = record["outbound"], record.get("returnLeg")
    require(
        now() <= start and now() < timestamp(outbound["pickupAt"])
        and start <= timestamp(outbound["pickupAt"])
        and end >= timestamp((inbound or outbound)["estimatedArrival"]),
        "The reservation must cover the complete upcoming itinerary", 409,
    )

def validate_allocation(db, row, quote, *, lock=False):
    start, end = timestamp(quote["start"]), timestamp(quote["end"])
    validate_window(row.data, start, end)
    ids = quote["vehicleIds"]
    require(len(ids) == len(set(ids)), "Select each vehicle once", 400)
    require(row.data["allowSplit"] or len(ids) == 1, "The customer requested one vehicle", 409)
    query = select(VEHICLES).where(
        VEHICLES.id.in_(ids), VEHICLES.organisation_id == row.supplier_id,
        VEHICLES.data["category"].as_string() == "PASSENGER",
    ).order_by(VEHICLES.id)
    vehicles = [entry.data for entry in db.scalars(query.with_for_update().execution_options(populate_existing=True) if lock else query)]
    require(len(vehicles) == len(ids), "Select vehicles from your passenger fleet", 404)
    driver_ids = {vehicle.get("driverId") for vehicle in vehicles if vehicle.get("driverId")}
    require(len(driver_ids) == sum(bool(vehicle.get("driverId")) for vehicle in vehicles),
            "Assign a different driver to every quoted vehicle", 409)
    if lock:
        list(db.scalars(select(User.id).where(User.id.in_(driver_ids)).order_by(User.id).with_for_update()))
    through = end.astimezone(SA).date()
    for vehicle in vehicles:
        issue = publication_issue(db, vehicle, row.supplier_id, through=through)
        require(not issue, issue or "", 409)
        availability = vehicle["availability"]
        require(timestamp(availability["start"]) <= start and timestamp(availability["end"]) >= end,
                "A selected vehicle is unavailable for these dates", 409)

    busy_vehicles, busy_drivers = occupied_fleet(db, row.id, start, end)
    require(not busy_vehicles.intersection(ids) and not busy_drivers.intersection(driver_ids),
            "A selected vehicle or driver is already booked during this window", 409)
    if not row.data.get("groupId"):
        validate_capacity(row.data, vehicles)
    return [
        {**{key: v[key] for key in ("id", "driverId", "registration", "type", "make", "model", "seats")},
         "version": v["version"]}
        for v in vehicles
    ]


def occupied_fleet(db, booking_id, start, end):
    vehicles, drivers = set(), set()
    for booking in db.scalars(confirmed_bookings().where(BOOKINGS.id != booking_id)):
        reserved = booking.data["quote"]
        if start < timestamp(reserved["end"]) and timestamp(reserved["start"]) < end:
            for vehicle in reserved["vehicles"]:
                vehicles.add(vehicle["id"])
                if vehicle.get("driverId"):
                    drivers.add(vehicle["driverId"])
    return vehicles, drivers


def validate_capacity(record, vehicles):
    for leg in filter(None, (record["outbound"], record.get("returnLeg"))):
        passenger_dispatch.allocate_seats(leg, vehicles)
        require(sum(v["luggageKg"] for v in vehicles) >= leg["luggageKg"]
                and sum(v["luggageM3"] for v in vehicles) >= leg["luggageM3"],
                "The selected vehicles cannot carry the declared luggage on one of the legs", 409)
        if bag := leg.get("largestBag"):
            require(any(
                v.get("luggageDimensions") and all(
                    required <= available for required, available in zip(
                        sorted(bag.values()), sorted(v["luggageDimensions"].values())
                    )
                ) for v in vehicles
            ), "The largest luggage item does not fit the selected vehicles", 409)


def fleet_candidates(db, query, start, end, busy):
    occupied, busy_drivers = busy
    through = end.astimezone(SA).date()
    for entry in db.scalars(query):
        vehicle = entry.data
        availability = vehicle["availability"]
        if (entry.id in occupied or vehicle.get("driverId") in busy_drivers
                or timestamp(availability["start"]) > start or timestamp(availability["end"]) < end):
            continue
        if not publication_issue(db, vehicle, entry.organisation_id, through=through):
            yield entry


def recommend_allocation(db, row, start, end):
    validate_window(row.data, start, end)
    occupied, busy_drivers = occupied_fleet(db, row.id, start, end)
    query = select(VEHICLES).where(
        VEHICLES.organisation_id == row.supplier_id,
        VEHICLES.data["category"].as_string() == "PASSENGER",
    )
    candidates = [entry.data for entry in fleet_candidates(db, query, start, end, (occupied, busy_drivers))]
    return [vehicle["id"] for vehicle in choose_allocation(row.data, candidates)]


def choose_allocation(record, candidates, provider_priority=None, *, fixed=()):
    def fits(group):
        group = [*fixed, *group]
        if len(group) > 20 or len({v["driverId"] for v in group}) != len(group):
            return False
        try:
            validate_capacity(record, group)
        except AppError:
            return False
        return True

    def rank_single(vehicle):
        return vehicle["type"] != "MINIBUS", vehicle["seats"], vehicle["id"] != record.get("vehicleId"), vehicle["id"]

    for candidate in sorted(candidates, key=rank_single):
        if fits([candidate]):
            return [candidate]
    if not record["allowSplit"]:
        return []

    # Try small combinations and larger capacity-first plans. Bound the search
    # so a fleet with hundreds of vehicles cannot exhaust a request worker.
    def rank_plan(group):
        if provider_priority:
            ranks = sorted({provider_priority[v["id"]] for v in group})
            return len(ranks), ranks, len(group), sum(v["seats"] for v in group), tuple(sorted(v["id"] for v in group))
        return len(group), sum(v["seats"] for v in group), tuple(sorted(v["id"] for v in group))

    plans = []
    for count in (2, 3):
        plans.extend(group for group in islice(combinations(sorted(candidates, key=rank_single), count), 1500) if fits(group))
        if plans:
            break
    priorities = ["seats", "wheelchairPositions", "luggageKg", "luggageM3"]
    if provider_priority:
        priorities.insert(0, "provider")
    for priority in priorities:
        group, drivers = [], set()
        def rank_vehicle(vehicle):
            value = provider_priority[vehicle["id"]] if priority == "provider" else -vehicle[priority]
            return value, -vehicle["seats"], vehicle["id"]

        for vehicle in sorted(candidates, key=rank_vehicle):
            if vehicle["driverId"] in drivers:
                continue
            group.append(vehicle)
            if vehicle.get("driverId"):
                    drivers.add(vehicle["driverId"])
            if fits(group):
                plans.append(group)
                break
            if len(group) == 20:
                break
    if not plans:
        return []
    return list(min(plans, key=rank_plan))


def itinerary(data):
    outbound = leg_terms(data.outbound)
    inbound = leg_terms(data.returnLeg) if data.returnLeg else None
    require(timestamp(outbound["pickupAt"]) > now(), "Choose a future pickup time", 400)
    require(not inbound or timestamp(inbound["pickupAt"]) > timestamp(outbound["estimatedArrival"]),
            "Return pickup must follow the estimated outbound arrival", 400)
    return {"outbound": outbound, "returnLeg": inbound, "allowSplit": data.allowSplit}


def available_company_fleets(db, org, record, supplier_id=None):
    start = timestamp(record["outbound"]["pickupAt"])
    end = timestamp((record["returnLeg"] or record["outbound"])["estimatedArrival"])
    query = select(VEHICLES).where(
        VEHICLES.organisation_id != org,
        VEHICLES.data["category"].as_string() == "PASSENGER",
        VEHICLES.data["published"].as_boolean().is_(True),
    ).order_by(VEHICLES.organisation_id, VEHICLES.id)
    if supplier_id is not None:
        query = query.where(VEHICLES.organisation_id == supplier_id)
    candidates = fleet_candidates(db, query, start, end, occupied_fleet(db, None, start, end))
    return {company: [entry.data for entry in fleet]
            for company, fleet in groupby(candidates, key=lambda entry: entry.organisation_id)}


def public_vehicle(vehicle):
    keys = ("id", "type", "make", "model", "seats", "wheelchairPositions", "displacedSeats", "luggageKg", "luggageM3")
    return {**{key: vehicle[key] for key in keys}, **subtype_details(vehicle)}


def company_plan(db, company_id, vehicles):
    return {"supplier": db.get(Organisation, company_id).name,
            "vehicleId": vehicles[0]["id"], "vehicles": [public_vehicle(vehicle) for vehicle in vehicles]}


def compare_providers(db, org, record, supplier_id=None, *, allow_multiple=False, include_gap=False):
    fleets = available_company_fleets(db, org, record, supplier_id)
    matches = []
    for company_id, fleet in fleets.items():
        plan = choose_allocation(record, fleet)
        if plan:
            matches.append(company_plan(db, company_id, plan))
    if matches or not allow_multiple:
        return sorted(matches, key=lambda match: (
            len(match["vehicles"]),
            match["vehicles"][0]["type"] != "MINIBUS" if len(match["vehicles"]) == 1 else False,
            sum(v["seats"] for v in match["vehicles"]), match["supplier"].casefold(), match["vehicleId"],
        ))

    def fleet_rank(company):
        # Vehicles sharing a driver contribute the driver's best usable capacity.
        capacity = {}
        for v in fleets[company]:
            capacity[v["driverId"]] = max(capacity.get(v["driverId"], 0), v["seats"])
        return -sum(capacity.values()), company

    companies = sorted(fleets, key=fleet_rank)
    owners = {v["id"]: company for company, vehicles in fleets.items() for v in vehicles}
    ranks = {v["id"]: rank for rank, company in enumerate(companies) for v in fleets[company]}
    plan = choose_allocation(record, [v for company in companies for v in fleets[company]], ranks)
    allocations = [company_plan(db, company, selected) for company in companies
                   if (selected := [v for v in plan if owners[v["id"]] == company])]
    if len(allocations) < 2:
        return [{"gaps": capacity_gaps(record, [v for fleet in fleets.values() for v in fleet])}] if include_gap else []
    return [{
        "supplier": "Combined fleet", "vehicleId": plan[0]["id"],
        "vehicles": [public_vehicle(vehicle) for vehicle in plan], "allocations": allocations,
    }]


def capacity_gaps(record, fleet):
    # Report a lower bound on shortages across the available pool. Driver
    # sharing and competing seat/wheelchair configurations can raise a gap.
    def capacity(key):
        by_driver = {}
        for vehicle in fleet:
            driver = vehicle["driverId"]
            by_driver[driver] = max(by_driver.get(driver, 0), vehicle[key])
        return sum(by_driver.values())

    return [
        {
            "leg": label,
            "passengers": max(0, leg["passengers"] - leg["wheelchairPositions"] - capacity("seats")),
            "wheelchairPositions": max(0, leg["wheelchairPositions"] - capacity("wheelchairPositions")),
            "luggageKg": round(max(0, leg["luggageKg"] - capacity("luggageKg")), 2),
            "luggageM3": round(max(0, leg["luggageM3"] - capacity("luggageM3")), 2),
            "largestBag": bool(leg.get("largestBag") and not any(
                v.get("luggageDimensions") and all(
                    required <= available for required, available in zip(
                        sorted(leg["largestBag"].values()), sorted(v["luggageDimensions"].values())
                    )
                ) for v in fleet
            )),
        }
        for label, leg in (("Outbound", record["outbound"]), ("Return", record["returnLeg"])) if leg
    ]


def booking_record(db, party, org, supplier_id, ident, vehicle, trip, data, extra=None):
    record = {
        "id": ident, "vehicleId": vehicle["id"], "vehicleName": f'{vehicle["make"]} {vehicle["model"]}',
        "customer": db.get(Organisation, org).name, "supplier": db.get(Organisation, supplier_id).name,
        **trip, **(extra or {}),
        "contactName": data.contactName.strip(), "contactPhone": data.contactPhone.strip(),
        "notes": data.notes.strip(), "status": "REQUESTED", "quote": None,
        "updatedAt": iso(), "version": new_id("REV"), "history": [{"status": "REQUESTED", "at": iso()}],
    }
    row = BOOKINGS(id=ident, owner_id=party.id, organisation_id=org, supplier_id=supplier_id, data=record)
    db.add(row)
    return row


def change_status(row, status, reason=""):
    record = {**row.data, "status": status, "version": new_id("REV"), "updatedAt": iso()}
    record["history"] = [*record["history"], {"status": status, "at": record["updatedAt"], "reason": reason}]
    row.data = record


def group_rows(db, org, group_id, *, lock=False):
    query = select(BOOKINGS).where(
        BOOKINGS.organisation_id == org, BOOKINGS.data["groupId"].as_string() == group_id,
    ).order_by(BOOKINGS.id)
    return list(db.scalars(query.with_for_update().execution_options(populate_existing=True) if lock else query))


def request_group(db, party, org, data):
    group_id = "CH-" + str(data.id)
    db.get(Organisation, org, with_for_update=True)
    existing = group_rows(db, org, group_id)
    if existing:
        return {"id": group_id}
    require(db.get(BOOKINGS, group_id) is None, "Request not found", 404)
    require(data.allowSplit and data.allowMultipleProviders, "Allow multiple transporters before requesting a combined fleet", 400)
    require(data.contactName.strip() and data.contactPhone.strip(), "Enter your contact details", 400)
    trip = itinerary(data)
    matches = compare_providers(db, org, trip, allow_multiple=True)
    plan = next((match for match in matches if match.get("allocations")
                 and sorted(data.vehicleIds) == sorted(v["id"] for v in match["vehicles"])), None)
    require(plan, "The combined fleet changed. Compare transporters again.", 409)
    for index, allocation in enumerate(plan["allocations"]):
        vehicle_ids = [v["id"] for v in allocation["vehicles"]]
        row = booking_record(
            db, party, org, db.get(VEHICLES, vehicle_ids[0]).organisation_id,
            group_id if index == 0 else new_id("CH"), allocation["vehicles"][0], trip, data,
            {"groupId": group_id, "suggestedVehicles": allocation["vehicles"]},
        )
        notify(db, row, row.supplier_id, "New combined charter allocation", ("TRANSPORTER", "DISPATCHER"))
    return {"id": group_id}


def passenger_manifest(record, vehicles):
    return {key: passenger_dispatch.allocate_seats(record[key], vehicles, build_manifest=True)
            for key in ("outbound", "returnLeg") if record.get(key)}


def transition_group(db, org, data):
    rows = group_rows(db, org, data.id, lock=True)
    require(len(rows) >= 2, "Combined charter not found", 404)
    if data.action == "CONFIRM" and all(row.data["status"] == "CONFIRMED" for row in rows):
        return {"id": data.id}
    require({row.id: row.data["version"] for row in rows} == data.versions,
            "A quote changed. Review the latest combined plan.", 409)
    require(timestamp(rows[0].data["outbound"]["pickupAt"]) > now(), "The charter has already started", 409)
    if data.action == "CONFIRM":
        require(all(row.data["status"] == "QUOTED" and timestamp(row.data["quote"]["validUntil"]) > now()
                    for row in rows), "Every transporter must provide a current quote", 409)
        snapshots = [v for row in rows for v in row.data["quote"]["vehicles"]]
        ids = [v["id"] for v in snapshots]
        drivers = {v["driverId"] for v in snapshots if v.get("driverId")}
        require(len(ids) <= 20 and len(set(ids)) == len(ids) and len(drivers) == sum(bool(v.get("driverId")) for v in snapshots),
                "The combined plan needs distinct vehicles and drivers, with at most 20 vehicles", 409)
        # Same lock order as single bookings. Validate after the locks to catch
        # a competing confirmation or an edit committed while waiting.
        vehicles = list(db.scalars(select(VEHICLES).where(VEHICLES.id.in_(ids)).order_by(VEHICLES.id).with_for_update()))
        list(db.scalars(select(User.id).where(User.id.in_(drivers)).order_by(User.id).with_for_update()))
        for row in rows:
            quote = row.data["quote"]
            try:
                current = validate_allocation(db, row, quote)
                require(current == quote["vehicles"], "Vehicles or drivers changed; request a revised quote", 409)
            except AppError as exc:
                raise AppError(f'{row.data["supplier"]}: {exc.message}', exc.status) from exc
        fleet = [v.data for v in vehicles]
        validate_capacity(rows[0].data, fleet)
        manifest = passenger_manifest(rows[0].data, fleet)
    else:
        require(data.reason.strip(), "Enter a cancellation reason", 400)
    status = "CONFIRMED" if data.action == "CONFIRM" else "CANCELLED"
    for row in rows:
        previously_confirmed = row.data["status"] == "CONFIRMED"
        change_status(row, status, data.reason.strip())
        if status == "CONFIRMED":
            passenger_dispatch.initialise_jobs(db, row, manifest)
        elif previously_confirmed:
            passenger_dispatch.cancel_jobs(db, row)
        notify(db, row, row.supplier_id, "Combined charter " + status.lower(), ("TRANSPORTER", "DISPATCHER"))
    return {"id": data.id}


def replace_provider(db, org, data, *, apply=False):
    current = db.get(BOOKINGS, data.id)
    require(current and current.organisation_id == org and current.data.get("groupId"),
            "Combined allocation not found", 404)
    rows = group_rows(db, org, current.data["groupId"], lock=apply)
    row = next(entry for entry in rows if entry.id == data.id)
    require(row.data["version"] == data.version, "The allocation changed. Review the latest plan.", 409)
    require(all(entry.data["status"] in {"REQUESTED", "QUOTED", "DECLINED"} for entry in rows)
            and timestamp(row.data["outbound"]["pickupAt"]) > now(),
            "This combined charter can no longer change providers", 409)
    fixed_ids = [
        vehicle["id"]
        for entry in rows if entry.id != row.id
        for vehicle in (entry.data["quote"] or {}).get("vehicles", entry.data["suggestedVehicles"])
    ]
    fixed = [vehicle.data for vehicle in db.scalars(select(VEHICLES).where(VEHICLES.id.in_(fixed_ids)))]
    require(len(fixed) == len(fixed_ids), "Another allocation needs to be revised first", 409)
    assigned = {entry.supplier_id for entry in rows}
    fleets = available_company_fleets(db, org, row.data)
    options = [
        company_plan(db, company, plan)
        for company, fleet in fleets.items()
        if company not in assigned and (plan := choose_allocation(row.data, fleet, fixed=fixed))
    ]
    options.sort(key=lambda option: (len(option["vehicles"]), sum(v["seats"] for v in option["vehicles"]),
                                     option["supplier"].casefold(), option["vehicleId"]))
    if not apply:
        return options
    selected = next((option for option in options
                     if sorted(data.vehicleIds) == sorted(v["id"] for v in option["vehicles"])), None)
    require(selected, "This replacement is no longer available. Find replacements again.", 409)
    old_supplier = row.supplier_id
    reason = f'{row.data["supplier"]} replaced by {selected["supplier"]}'
    vehicle = selected["vehicles"][0]
    row.supplier_id = db.get(VEHICLES, vehicle["id"]).organisation_id
    history = row.data["history"]
    if row.data.get("quote"):
        history = [*history, {"status": "QUOTE_WITHDRAWN", "at": iso(), "quote": row.data["quote"]}]
    row.data = {
        **row.data, "history": history, "supplier": selected["supplier"], "vehicleId": vehicle["id"],
        "vehicleName": f'{vehicle["make"]} {vehicle["model"]}',
        "suggestedVehicles": selected["vehicles"], "quote": None,
    }
    change_status(row, "REQUESTED", reason)
    # The previous supplier is removed from the allocation's access scope.
    notify_organisation(db, old_supplier, title="Combined charter allocation withdrawn",
                        detail="The customer selected a replacement transporter.",
                        subject_type="CHARTER_BOOKING", subject_id=row.id,
                        role_ids=[ROLE_IDS["TRANSPORTER"], ROLE_IDS["DISPATCHER"]])
    notify(db, row, row.supplier_id, "New combined charter allocation", ("TRANSPORTER", "DISPATCHER"))
    return {"id": row.id}


def public_quote(quote):
    return {
        **quote,
        "vehicles": [{key: value for key, value in vehicle.items() if key not in {"driverId", "version"}}
                     for vehicle in quote["vehicles"]],
    }


def response_for(row, org, db=None):
    record = dict(row.data)
    record.pop("driverJobs", None)
    record["vehicleProgress"] = passenger_dispatch.vehicle_progress(row)
    if db is not None and row.supplier_id == org:
        record["driverAssignments"] = passenger_dispatch.supplier_assignments(db, row)
    record["history"] = [
        {key: value for key, value in event.items() if key != "quote"}
        if row.supplier_id == org else
        {**event, "quote": public_quote(event["quote"])} if event.get("quote") else event
        for event in record["history"]
    ]
    if quote := record.get("quote"):
        record["quote"] = public_quote(quote)
    return {**record, "incoming": row.supplier_id == org}


def notify(db, row, org, title, recipient_roles):
    notify_organisation(
        db, org, title=title, detail=row.data["outbound"]["origin"]["label"],
        subject_type="CHARTER_BOOKING", subject_id=row.id,
        role_ids=[ROLE_IDS[role] for role in recipient_roles],
    )


def handle(action, data, db, request, response, party):
    if action in {"driverOptions", "reassignDriver"}:
        return passenger_dispatch.reassign_driver(db, party, data, save=action == "reassignDriver")
    if action in {"driverJobs", "respondJob", "advanceJob", "publishPosition"}:
        return passenger_dispatch.handle(action, data, db, party)
    org = db.info.get("active_organisation_id")
    role = party.data.get("role")
    require(org and role in {"FREIGHT_OWNER", "TRANSPORTER"}, "Select your organisation", 403)
    require_permission(db, party, "loads.read")
    if action == "rate":
        require_permission(db, party, "loads.manage" if role == "FREIGHT_OWNER" else "matches.manage")
        row = db.get(BOOKINGS, data.bookingId, with_for_update=True)
        require(row and row.organisation_id == org, "Charter not found", 404)
        require(row.data["status"] == "CONFIRMED" and passenger_dispatch.vehicle_complete(row.data, data.vehicleId),
                "Complete this vehicle's entire journey before rating", 409)
        ratings = dict(row.data.get("ratings", {}))
        require(data.vehicleId not in ratings, "This journey has already been rated", 409)
        vehicle = next(v for v in row.data["quote"]["vehicles"] if v["id"] == data.vehicleId)
        ratings[data.vehicleId] = {"stars": data.stars, "comment": data.comment.strip(),
                                  "at": iso(), "byUserId": party.id, "driverId": vehicle.get("driverId")}
        row.data = {**row.data, "ratings": ratings, "history": [*row.data.get("history", []),
                    {"status": "JOURNEY_RATED", "at": iso(), "reason": f'{vehicle["registration"]} · {data.stars}/5'}]}
        notify(db, row, row.supplier_id, "Passenger journey rated", ("TRANSPORTER", "DISPATCHER"))
        return {"ok": True}

    if action == "live":
        require_permission(db, party, "tracking.read")
        row = db.get(BOOKINGS, data.bookingId)
        require(row and org in {row.organisation_id, row.supplier_id}, "Charter not found", 404)
        group_id = row.data.get("groupId")
        rows = group_rows(db, org, group_id) if group_id and org == row.organisation_id else [row]
        return [vehicle for entry in rows for vehicle in passenger_dispatch.live_vehicles(entry)]

    if action == "list":
        return [response_for(row, org, db) for row in db.scalars(
            select(BOOKINGS).where(or_(BOOKINGS.organisation_id == org, BOOKINGS.supplier_id == org))
            .order_by(BOOKINGS.data["updatedAt"].as_string().desc(), BOOKINGS.id)
        )]

    if action == "search":
        require_permission(db, party, "loads.manage" if role == "FREIGHT_OWNER" else "matches.manage")
        require(not data.allowMultipleProviders or data.allowSplit, "Allow multiple vehicles before combining transporters", 400)
        return compare_providers(db, org, itinerary(data), allow_multiple=data.allowMultipleProviders, include_gap=True)

    if action in {"replacementOptions", "replaceProvider"}:
        require_permission(db, party, "loads.manage" if role == "FREIGHT_OWNER" else "matches.manage")
        return replace_provider(db, org, data, apply=action == "replaceProvider")

    if action in {"requestGroup", "transitionGroup"}:
        require_permission(db, party, "loads.manage" if role == "FREIGHT_OWNER" else "matches.manage")
        return request_group(db, party, org, data) if action == "requestGroup" else transition_group(db, org, data)

    if action == "request":
        require_permission(db, party, "loads.manage" if role == "FREIGHT_OWNER" else "matches.manage")
        # The request ID makes a retry after a lost response safe.
        ident = "CH-" + str(data.id)
        db.get(Organisation, org, with_for_update=True)
        previous = db.get(BOOKINGS, ident)
        if previous:
            require(previous.organisation_id == org, "Request not found", 404)
            return response_for(previous, org)
        vehicle = db.get(VEHICLES, data.vehicleId)
        require(vehicle and vehicle.organisation_id != org and vehicle.data.get("category") == "PASSENGER"
                and vehicle.data.get("published")
                and not publication_issue(db, vehicle.data, vehicle.organisation_id),
                "This vehicle is no longer available for requests", 409)
        trip = itinerary(data)
        if data.requireFleetMatch:
            require(bool(compare_providers(db, org, trip, vehicle.organisation_id)),
                    "This company can no longer cover the itinerary. Compare transporters again.", 409)
        require(data.contactName.strip() and data.contactPhone.strip(), "Enter your contact details", 400)
        row = booking_record(db, party, org, vehicle.organisation_id, ident, vehicle.data, trip, data)
        notify(db, row, row.supplier_id, "New passenger charter request", ("TRANSPORTER", "DISPATCHER"))
        return response_for(row, org)

    if action == "recommend":
        require_permission(db, party, "trucks.manage")
        row = db.get(BOOKINGS, data.id)
        require(row and row.supplier_id == org, "Charter request not found", 404)
        require(row.data["version"] == data.version and row.data["status"] in {"REQUESTED", "QUOTED"},
                "Refresh the charter request before selecting vehicles", 409)
        if row.data.get("groupId"):
            ids = [v["id"] for v in row.data["suggestedVehicles"]]
            quote = {"vehicleIds": ids, "start": data.start.isoformat(), "end": data.end.isoformat()}
            try:
                validate_allocation(db, row, quote)
            except AppError:
                return {"vehicleIds": []}
            return {"vehicleIds": ids}
        return {"vehicleIds": recommend_allocation(db, row, data.start, data.end)}

    row = db.get(BOOKINGS, data.id, with_for_update=True)
    require(row and org in {row.organisation_id, row.supplier_id}, "Charter request not found", 404)
    incoming = org == row.supplier_id
    require_permission(db, party, "trucks.manage" if incoming else "loads.manage" if role == "FREIGHT_OWNER" else "matches.manage")
    record = dict(row.data)
    previously_confirmed = record["status"] == "CONFIRMED"
    require(record["version"] == data.version, "This charter changed. Refresh and review the latest details.", 409)
    if action == "quote":
        require(incoming and record["status"] in {"REQUESTED", "QUOTED"}, "This request cannot be quoted", 409)
        require(now() < data.validUntil <= data.start,
                "Quote expiry must be after now and on or before the reservation starts", 400)
        for leg_key, deadline in (("outbound", data.outboundArrivalBy), ("returnLeg", data.returnArrivalBy)):
            if deadline is None:
                continue
            leg = record.get(leg_key)
            require(leg and timestamp(leg["estimatedArrival"]) <= deadline <= data.end,
                    "An agreed drop-off must be on or after the estimated arrival and within the reservation", 400)
            if leg_key == "outbound" and record.get("returnLeg"):
                require(deadline <= timestamp(record["returnLeg"]["pickupAt"]),
                        "Outbound drop-off must be on or before return pickup", 400)
        quote = data.model_dump(mode="json", exclude={"version", "id"})
        quote["vehicles"] = validate_allocation(db, row, quote)
        if record.get("quote"):
            record["history"] = [*record["history"], {
                "status": "QUOTE_REVISED", "at": iso(), "quote": record["quote"],
            }]
        record.update(status="QUOTED", quote=quote)
    elif data.action == "CONFIRM":
        require(not record.get("groupId") and not incoming and record["status"] == "QUOTED", "This quote cannot be accepted", 409)
        quote = record["quote"]
        require(timestamp(quote["validUntil"]) > now(), "The quote expired. Ask the transporter for a new quote.", 409)
        require(validate_allocation(db, row, quote, lock=True) == quote["vehicles"],
                "The quoted vehicles or drivers changed. Ask the transporter for a new quote.", 409)
        record["status"] = "CONFIRMED"
    else:
        require(not record.get("groupId") or (incoming and data.action == "DECLINE"),
                "Use the combined charter actions for this booking", 409)
        require(record["status"] in {"REQUESTED", "QUOTED", "CONFIRMED"}
                and timestamp(record["outbound"]["pickupAt"]) > now(),
                "This charter can no longer be changed", 409)
        require((incoming and data.action == "DECLINE" and record["status"] != "CONFIRMED") or data.action == "CANCEL",
                "This action is unavailable", 409)
        require(data.reason.strip(), "Enter a reason", 400)
        record["status"] = "DECLINED" if data.action == "DECLINE" else "CANCELLED"
    record["version"] = new_id("REV")
    record["updatedAt"] = iso()
    record["history"] = [*record["history"], {
        "status": record["status"], "at": record["updatedAt"],
        "reason": getattr(data, "reason", "").strip(),
    }]
    row.data = record
    if action == "transition" and data.action == "CONFIRM":
        fleet = [db.get(VEHICLES, v["id"]).data for v in record["quote"]["vehicles"]]
        passenger_dispatch.initialise_jobs(db, row, passenger_manifest(record, fleet))
    elif previously_confirmed and record["status"] == "CANCELLED":
        passenger_dispatch.cancel_jobs(db, row)
    notify(db, row, row.organisation_id if incoming else row.supplier_id,
           "Passenger charter " + record["status"].lower(),
           ("FREIGHT_OWNER", "TRANSPORTER", "DISPATCHER", "CARGO_OPERATOR"))
    return response_for(row, org)
