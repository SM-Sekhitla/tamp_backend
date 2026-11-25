from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from sqlalchemy import or_, select

from app.core.errors import require
from app.core.security import iso, new_id
from app.db.models import MODELS, Organisation
from app.schemas.domain import LoadInput
from app.services.domain import audit, lock_domain, refresh_suggestions, sanitize_load
from app.services.finance import FREIGHT_VAT_RATE
from app.services.notifications import notify_organisation
from app.services.rbac import require_permission
from app.services.records import needed, put
from app.services.storage import own_upload


def _bookings(db, asset_id):
    model = MODELS["assetBookings"]
    return db.scalars(select(model).where(model.asset_id == asset_id)).all()


def _overlaps(first, second):
    return (
        datetime.fromisoformat(first["start"]) < datetime.fromisoformat(second["end"])
        and datetime.fromisoformat(second["start"]) < datetime.fromisoformat(first["end"])
    )


def _reserved(booking):
    return booking["status"] in {"CONFIRMED", "IN_HIRE", "COMPLETED"}


def _available(asset, period, bookings):
    window = asset["availability"]
    require(
        datetime.fromisoformat(window["start"]) <= datetime.fromisoformat(period["start"])
        and datetime.fromisoformat(period["end"]) <= datetime.fromisoformat(window["end"]),
        "Choose dates within the asset's availability", 409,
    )
    blocked = [*asset["blockedPeriods"], *(row.data for row in bookings if _reserved(row.data))]
    require(not any(_overlaps(period, item) for item in blocked), "The asset is unavailable during those dates", 409)


def _notify(db, booking, organisation_id):
    notify_organisation(
        db, organisation_id, title="Asset hire " + booking["status"].lower().replace("_", " "),
        detail=booking["assetName"], subject_type="ASSET_HIRE", subject_id=booking["id"],
    )


def _rental_blocker(booking, asset_in_use=False):
    status = booking["status"]
    if status == "CONFIRMED":
        if asset_in_use:
            return "This asset is still out on another rental. Complete its return before starting this rental."
        if datetime.fromisoformat(booking["start"]) > datetime.now(timezone.utc):
            return "You can start at the scheduled rental start time."
        stage = "DELIVERY"
    elif status == "IN_HIRE":
        stage = "RETURN"
    else:
        return None

    reports = booking.get("handovers", {}).get(stage, {})
    if not reports.get("OWNER") or not reports.get("RENTER"):
        return f"The asset owner and renter must both record {stage.lower()} before you can {'start' if stage == 'DELIVERY' else 'complete'} this rental."
    return None


def _list(db, org):
    assets = MODELS["hireAssets"]
    bookings = MODELS["assetBookings"]
    assets_list = db.scalars(select(assets).where(or_(
        assets.organisation_id == org, assets.data["published"].as_boolean().is_(True),
    ))).all()
    booking_rows = db.scalars(select(bookings).where(or_(
        bookings.organisation_id == org, bookings.supplier_id == org,
    ))).all()
    in_use = {row.asset_id for row in booking_rows if row.supplier_id == org and row.data["status"] == "IN_HIRE"}
    displayed_ids = [row.id for row in assets_list]
    reservations = db.scalars(select(bookings).where(bookings.asset_id.in_(displayed_ids))).all()
    reserved_by_asset = {}
    for row in reservations:
        if _reserved(row.data):
            reserved_by_asset.setdefault(row.asset_id, []).append({
                "start": row.data["start"], "end": row.data["end"],
            })
    moves = MODELS["assetMoves"]
    loads = MODELS["loads"]
    move_rows = db.execute(
        select(moves, loads).join(loads, loads.id == moves.id).where(moves.organisation_id == org)
    ).all()
    return {
        "moves": [
            {**move.data, "status": load.data["status"], "destination": load.data["destination"]["label"]}
            for move, load in move_rows
        ],
        "assets": [
            {**row.data, "mine": row.organisation_id == org, "reservations": reserved_by_asset.get(row.id, [])}
            for row in assets_list
        ],
        "bookings": [{
            **row.data, "incoming": row.supplier_id == org,
            "transitionBlocker": _rental_blocker(row.data, row.asset_id in in_use) if row.supplier_id == org else None,
        } for row in booking_rows],
    }


def _invoice(booking):
    cents = Decimal("0.01")
    vat_rate = Decimal(str(FREIGHT_VAT_RATE))
    amount = Decimal(booking["total"])
    if booking["terms"]["vat"] == "incl":
        total = amount
        subtotal = (total / (1 + vat_rate)).quantize(cents, rounding=ROUND_HALF_UP)
        vat = total - subtotal
    else:
        subtotal = amount
        vat = (subtotal * vat_rate).quantize(cents, rounding=ROUND_HALF_UP)
        total = subtotal + vat
    return {
        "number": "AR-" + booking["id"].removeprefix("HIRE-").upper(),
        "issuedAt": iso(), "currency": "ZAR", "subtotal": str(subtotal),
        "vatRate": str(vat_rate), "vat": str(vat), "total": str(total),
        "paid": "0.00", "balance": str(total), "status": "OUTSTANDING",
        "payments": [],
    }


def _billing(action, data, db, party, org):
    row = needed(db, "assetBookings", data.id, True)
    require(row.supplier_id == org, "Only the asset owner can manage rental billing", 403)
    require_permission(db, party, "trucks.manage")
    require(row.data["status"] == "COMPLETED", "Complete the rental before billing", 409)
    invoice = row.data.get("invoice")
    if action == "issueInvoice":
        if invoice:
            return invoice
        invoice = _invoice(row.data)
        title = "Asset rental invoice issued"
        event = "ASSET_RENTAL_INVOICE_ISSUED"
    else:
        require(invoice is not None, "Issue the rental invoice first", 409)
        reference = data.reference.strip()
        require(reference, "Enter a payment reference", 400)
        existing = next((item for item in invoice["payments"] if item["reference"].casefold() == reference.casefold()), None)
        if existing:
            require(Decimal(existing["amount"]) == data.amount, "This payment reference is already recorded", 409)
            return invoice
        balance = Decimal(invoice["balance"]) - data.amount
        require(balance >= 0, "Payment exceeds the outstanding balance", 400)
        payment = {
            "id": new_id("RP"), "amount": format(data.amount, ".2f"),
            "reference": reference, "recordedAt": iso(), "recordedBy": party.id,
        }
        invoice = {
            **invoice, "paid": format(Decimal(invoice["paid"]) + data.amount, ".2f"),
            "balance": format(balance, ".2f"), "status": "PAID" if balance == 0 else "PARTIALLY_PAID",
            "payments": [*invoice["payments"], payment],
        }
        title = "Asset rental payment recorded"
        event = "ASSET_RENTAL_PAYMENT_RECORDED"
    row.data = {**row.data, "invoice": invoice}
    audit(db, party, event, "ASSET_HIRE", row.id, title)
    notify_organisation(
        db, row.organisation_id, title=title, detail=row.data["assetName"],
        subject_type="ASSET_HIRE", subject_id=row.id,
    )
    return invoice


def handle(action, data, db, request, response, party):
    role = party.data["role"]
    org = db.info.get("active_organisation_id")
    require(role in {"FREIGHT_OWNER", "TRANSPORTER"} and org, "Select a freight or transporter organisation", 403)
    booking_permission = "loads.manage" if role == "FREIGHT_OWNER" else "matches.manage"
    if action == "list":
        require_permission(db, party, "loads.read")
        return _list(db, org)

    values = data.model_dump(mode="json", by_alias=True)
    if action == "save":
        require(role == "TRANSPORTER")
        require_permission(db, party, "trucks.manage")
        ident = values.pop("id") or new_id("ASSET")
        if data.id:
            row = needed(db, "hireAssets", ident, True)
            require(row.organisation_id == org, "Asset not found", 404)
        existing_photos = row.data["photos"] if data.id else []
        for url in set(data.photos) - set(existing_photos):
            own_upload(db, party, url, "ASSET_PHOTO")
        company = db.get(Organisation, org)
        record = {**values, "id": ident, "supplierName": company.name, "version": new_id("REV")}
        put(db, "hireAssets", record, party.id)
        audit(db, party, "ASSET_SAVED", "ASSET", ident, "Hire listing saved")
        return record

    if action == "request":
        require_permission(db, party, booking_permission)
        asset = needed(db, "hireAssets", data.assetId, True)
        require(asset.organisation_id != org and asset.data["published"], "Asset not available", 404)
        require(data.listingVersion == asset.data["version"], "The listing changed. Review the latest rate", 409)
        require(data.start > datetime.now(timezone.utc), "Choose a future start time", 400)
        _available(asset.data, values, _bookings(db, asset.id))
        unit_hours = {"HOUR": 1, "DAY": 24, "SHIFT": asset.data["shiftHours"]}[asset.data["unit"]]
        seconds = Decimal(str((data.end - data.start).total_seconds()))
        units = max(asset.data["minimumUnits"], int((seconds / (unit_hours * 3600)).to_integral_value(rounding=ROUND_CEILING)))
        record = {
            **values, "id": new_id("HIRE"), "status": "REQUESTED",
            "assetLocation": asset.data["location"],
            "assetName": f"{asset.data['make']} {asset.data['model']}",
            "supplierId": asset.organisation_id,
            "supplierName": asset.data["supplierName"],
            "renterName": db.get(Organisation, org).name,
            "units": units, "total": str(Decimal(asset.data["rate"]) * units),
            "terms": {key: asset.data[key] for key in ("rate", "unit", "minimumUnits", "shiftHours", "vat", "operatorIncluded", "fuelIncluded", "transportWeightKg", "dimensions")},
            "history": [{"status": "REQUESTED", "by": party.id, "at": iso()}],
        }
        db.add(MODELS["assetBookings"](
            id=record["id"], data=record, owner_id=party.id,
            organisation_id=org, asset_id=asset.id, supplier_id=asset.organisation_id,
        ))
        audit(db, party, "ASSET_HIRE_REQUESTED", "ASSET_HIRE", record["id"], "Hire requested")
        _notify(db, record, asset.organisation_id)
        return record

    if action == "transition":
        initial = needed(db, "assetBookings", data.id)
        require(org in {initial.organisation_id, initial.supplier_id}, "Booking not found", 404)
        asset = needed(db, "hireAssets", initial.asset_id, True)
        row = needed(db, "assetBookings", data.id, True)
        # The row may have been loaded before acquiring the asset lock.
        db.refresh(row)
        incoming = row.supplier_id == org
        require_permission(db, party, "trucks.manage" if incoming else booking_permission)
        require(incoming or data.action == "CANCEL", "Only the asset owner can perform this action", 403)
        transitions = {
            "CONFIRM": ({"REQUESTED"}, "CONFIRMED"),
            "DECLINE": ({"REQUESTED"}, "DECLINED"),
            "CANCEL": ({"REQUESTED", "CONFIRMED"}, "CANCELLED"),
            "START": ({"CONFIRMED"}, "IN_HIRE"),
            "COMPLETE": ({"IN_HIRE"}, "COMPLETED"),
        }
        allowed, status = transitions[data.action]
        if row.data["status"] == status:
            return row.data
        require(row.data["status"] in allowed, "The booking status has changed", 409)
        if data.action == "CONFIRM":
            require(asset.data["published"], "Publish the asset before confirming this booking", 409)
            require(datetime.fromisoformat(row.data["start"]) > datetime.now(timezone.utc), "The requested start time has passed", 409)
            _available(asset.data, row.data, _bookings(db, asset.id))
        if data.action in {"START", "COMPLETE"}:
            # Every transition for this machine takes the asset lock first.
            asset_in_use = data.action == "START" and any(
                other.id != row.id and other.data["status"] == "IN_HIRE"
                for other in _bookings(db, asset.id)
            )
            blocker = _rental_blocker(row.data, asset_in_use)
            require(blocker is None, blocker, 409)
        if data.action in {"DECLINE", "CANCEL"}:
            require(data.reason.strip(), "Enter a reason", 400)
        row.data = {
            **row.data, "status": status,
            "history": [*row.data["history"], {"status": status, "by": party.id, "at": iso(), "reason": data.reason.strip()}],
        }
        audit(db, party, "ASSET_HIRE_" + status, "ASSET_HIRE", row.id, data.reason.strip() or status)
        _notify(db, row.data, row.organisation_id if incoming else row.supplier_id)
        return row.data

    if action in {"issueInvoice", "recordPayment"}:
        return _billing(action, data, db, party, org)

    if action == "handover":
        row = needed(db, "assetBookings", data.id, True)
        require(org in {row.organisation_id, row.supplier_id}, "Rental not found", 404)
        incoming = row.supplier_id == org
        require_permission(db, party, "trucks.manage" if incoming else booking_permission)
        allowed = {"IN_HIRE", "COMPLETED"}
        if data.stage == "DELIVERY":
            allowed.add("CONFIRMED")
        require(row.data["status"] in allowed, "Handover is unavailable for this rental status", 409)
        require(
            set(data.checks) == {"assetIdentity", "conditionInspected", "attachmentsChecked"}
            and all(data.checks.values()),
            "Complete the handover checklist first", 400,
        )
        require(data.condition != "DAMAGE_FOUND" or data.notes.strip(), "Describe the damage", 400)
        side = "OWNER" if incoming else "RENTER"
        reports = row.data.get("handovers", {})
        stage = reports.get(data.stage, {})
        require(side not in stage, "Your organisation already recorded this handover", 409)
        for url in data.photos:
            own_upload(db, party, url, "ASSET_HANDOVER_PHOTO")
        report = {**values, "recordedAt": iso(), "recordedBy": party.id}
        row.data = {**row.data, "handovers": {**reports, data.stage: {**stage, side: report}}}
        title = f"Asset rental {data.stage.lower()} recorded"
        audit(db, party, "ASSET_HANDOVER_RECORDED", "ASSET_HIRE", row.id, title)
        notify_organisation(
            db, row.organisation_id if incoming else row.supplier_id,
            title=title, detail=row.data["assetName"], subject_type="ASSET_HIRE", subject_id=row.id,
        )
        return report

    if action == "move":
        require_permission(db, party, "loads.manage" if role == "FREIGHT_OWNER" else "trucks.manage")
        lock_domain(db)
        asset = needed(db, "hireAssets", data.assetId, True)
        terms = asset.data
        booking = None
        if data.bookingId:
            booking = needed(db, "assetBookings", data.bookingId, True)
            require(booking.asset_id == asset.id and org in {booking.organisation_id, booking.supplier_id}, "Booking not found", 404)
            require(booking.data["status"] in {"CONFIRMED", "IN_HIRE", "COMPLETED"}, "Confirm the hire before arranging transport", 409)
            prior = booking.data.get("moves", {}).get(data.direction)
            if prior:
                return {"id": prior}
            terms = booking.data["terms"]
        else:
            require(asset.organisation_id == org, "Asset not found", 404)
        require(terms["transportWeightKg"] <= 200000, "This asset exceeds the freight board’s 200 t limit", 400)
        require(data.pickupWindow.from_ > datetime.now(timezone.utc), "Choose a future pickup time", 400)
        require(data.deliveryBy.utcoffset() is not None and data.deliveryBy >= data.pickupWindow.to, "Delivery must follow pickup", 400)
        ident = new_id("LD")
        load = LoadInput(
            ownerId=party.id, cargoType="ABNORMAL", weightKg=terms["transportWeightKg"],
            origin=data.origin, destination=data.destination, distanceKm=0,
            pickupWindow=data.pickupWindow, deliveryBy=data.deliveryBy, specialRequirements=data.notes,
        ).model_dump(mode="json", by_alias=True, exclude_none=True)
        load = sanitize_load(db, party, {
            **load, "id": ident, "status": "POSTED", "assetMoveId": asset.id,
            "transportDimensions": terms["dimensions"],
        }, asset_move=True)
        put(db, "loads", load, party.id)
        put(db, "assetMoves", {"id": ident, "assetId": asset.id, "assetName": f"{asset.data['make']} {asset.data['model']}", "bookingId": data.bookingId, "direction": data.direction}, party.id)
        if booking:
            booking.data = {**booking.data, "moves": {**booking.data.get("moves", {}), data.direction: ident}}
        audit(db, party, "ASSET_MOVE_POSTED", "LOAD", ident, "Asset transport posted")
        refresh_suggestions(db)
        return {"id": ident}

    require(False, "Unknown asset hire action", 404)
