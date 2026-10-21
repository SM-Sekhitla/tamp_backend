from sqlalchemy import select

from app.core.errors import require
from app.db.models import MODELS, User


def rows(db, kind):
    return list(db.scalars(select(MODELS[kind])))


def get(db, kind, ident, lock=False):
    stmt = select(MODELS[kind]).where(MODELS[kind].id == ident)
    return db.scalar(stmt.with_for_update() if lock else stmt)


def needed(db, kind, ident, lock=False):
    row = get(db, kind, ident, lock)
    require(row is not None, "Record not found", 404)
    return row


def put(db, kind, data, owner=None):
    row = get(db, kind, data["id"])
    if row is None:
        organisation_id = db.info.get("active_organisation_id")
        if organisation_id is None and owner:
            from app.services.rbac import primary_organisation_id

            organisation_id = primary_organisation_id(db, db.get(User, owner))
        row = MODELS[kind](
            id=data["id"], owner_id=owner, organisation_id=organisation_id, data=data
        )
        db.add(row)
    else:
        row.data = dict(data)
    for column, key in {
        "vehicle_id": "vehicleId",
        "driver_id": "driverId",
        "load_id": "loadId",
        "truck_id": "truckPostingId" if kind == "matches" else "truckId",
        "trip_id": "tripId",
        "match_id": "matchId",
        "invoice_id": "invoiceId",
        "conversation_id": "conversationId",
    }.items():
        if hasattr(type(row), column):
            setattr(row, column, data[key])
    db.flush()
    return row
