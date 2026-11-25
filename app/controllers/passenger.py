"""Passenger fleet records and the published charter catalogue."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core.errors import require
from app.core.security import iso, new_id
from app.db.models import MODELS, Organisation, OrganisationMembership, User, UserRole
from app.schemas.passenger import PASSENGER_SUBTYPES
from app.services.driver_compliance import REQUIRED_DISPATCH_DOCUMENTS
from app.services.rbac import ROLE_IDS, has_permission, require_permission
from app.services.records import needed, put
from app.services.storage import own_upload

SA = ZoneInfo("Africa/Johannesburg")
VEHICLES = MODELS["vehicles"]
PUBLIC_FIELDS = (
    "id", "type", "make", "model", "seats", "wheelchairPositions", "displacedSeats",
    "luggageKg", "luggageM3", "luggageDimensions", "location", "serviceArea",
    "availability", "photos",
)
REVIEW_FIELDS = ("type", "subtype", "registration", "make", "model", "seats", "wheelchairPositions",
                 "displacedSeats", "licenceExpiry", "permitExpiry", "photos", "documents")


def subtype_details(data):
    subtype = data.get("subtype")
    return {"subtype": subtype, "subtypeLabel": PASSENGER_SUBTYPES[data["type"]].get(subtype)}


def passenger_rows():
    return select(VEHICLES).where(VEHICLES.data["category"].as_string() == "PASSENGER")


def drivers(db, org):
    return list(db.scalars(
        select(User)
        .join(OrganisationMembership, OrganisationMembership.user_id == User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            OrganisationMembership.organisation_id == org,
            OrganisationMembership.status == "ACTIVE",
            UserRole.organisation_id == org,
            UserRole.role_id == ROLE_IDS["DRIVER"],
        )
    ))


def publication_issue(db, data, org, *, through=None):
    now = datetime.now(SA)
    today = max(now.date(), through or now.date()).isoformat()
    company = db.get(Organisation, org)
    if not company or company.verification != "VERIFIED":
        return "Your organisation must be verified."
    if data.get("verification") != "VERIFIED":
        return "The vehicle must pass platform review."
    if min(data["licenceExpiry"], data["permitExpiry"]) < today:
        return "Renew the vehicle or operating licence."
    if datetime.fromisoformat(data["availability"]["end"]) <= now:
        return "Set a current availability window."
    driver = next((entry for entry in drivers(db, org) if entry.id == data.get("driverId")), None)
    if not driver or driver.data.get("verification") != "VERIFIED" or driver.data.get("suspended"):
        return "Assign a verified driver from your organisation."
    documents = MODELS["driverDocuments"]
    approved = {
        row.data["documentType"] for row in db.scalars(select(documents).where(
            documents.organisation_id == org, documents.driver_id == driver.id,
        ))
        if row.data.get("reviewStatus") == "APPROVED"
        and row.data.get("status") != "REMOVED"
        and (row.data.get("expiresAt") or "")[:10] >= today
    }
    if not REQUIRED_DISPATCH_DOCUMENTS.issubset(approved):
        return "The driver needs a current, approved driving licence and PrDP."
    return None


def can_read_file(db, party, url, kind):
    org = db.info.get("active_organisation_id")
    if not org:
        return False
    for row in db.scalars(passenger_rows()):
        data = row.data
        urls = data["photos"] if kind == "PASSENGER_PHOTO" else data["documents"].values()
        if url not in urls:
            continue
        if row.organisation_id == org and has_permission(db, party, "trucks.read"):
            return True
        if kind == "PASSENGER_PHOTO" and data["published"] and has_permission(db, party, "loads.read"):
            if not publication_issue(db, data, row.organisation_id):
                return True
    return False


def handle(action, data, db, request, response, party):
    org = db.info.get("active_organisation_id")
    if action == "reviews":
        require_permission(db, party, "platform.admin")
        return [{"vehicle": {**row.data, **subtype_details(row.data)}, "supplier": supplier(db, row)}
                for row in db.scalars(passenger_rows().order_by(VEHICLES.id))]
    if action == "review":
        require_permission(db, party, "platform.admin")
        row = needed(db, "vehicles", data.id, True)
        require(row.data.get("category") == "PASSENGER", "Passenger vehicle not found", 404)
        require(row.data["version"] == data.version, "Vehicle changed. Refresh before reviewing.", 409)
        require(data.status != "REJECTED" or data.reason.strip(), "Enter a reason for rejection", 400)
        updated = {
            **row.data, "verification": data.status, "reviewReason": data.reason.strip(),
            "reviewedBy": party.id, "reviewedAt": iso(), "version": new_id("REV"),
        }
        if data.status == "REJECTED":
            updated["published"] = False
        put(db, "vehicles", updated, row.owner_id)
        return {"id": row.id}

    role = party.data.get("role")
    require(org and role in {"TRANSPORTER", "FREIGHT_OWNER"}, "Select your organisation", 403)
    if action == "catalogue":
        require_permission(db, party, "loads.read")
        return [
            {**{key: row.data[key] for key in PUBLIC_FIELDS}, **subtype_details(row.data), "supplier": supplier(db, row), "mine": row.organisation_id == org}
            for row in db.scalars(passenger_rows().where(VEHICLES.data["published"].as_boolean().is_(True)))
            if not publication_issue(db, row.data, row.organisation_id)
        ]

    require(role == "TRANSPORTER")
    require_permission(db, party, "trucks.read" if action == "fleet" else "trucks.manage")
    if action == "fleet":
        return {
            "subtypes": PASSENGER_SUBTYPES,
            "vehicles": [
                {**row.data, **subtype_details(row.data), "publicationIssue": publication_issue(db, row.data, org)}
                for row in db.scalars(passenger_rows().where(VEHICLES.organisation_id == org))
            ],
            "drivers": [{"id": driver.id, "name": driver.data.get("contactName") or driver.email}
                        for driver in drivers(db, org)],
        }

    require(action == "save", "Passenger action not found", 404)
    # Serialize registration checks against concurrent passenger saves.
    db.get(Organisation, org, with_for_update=True)
    row = needed(db, "vehicles", data.id, True) if data.id else None
    if row:
        require(row.organisation_id == org and row.data.get("category") == "PASSENGER",
                "Passenger vehicle not found", 404)
    old = row.data if row else {}
    require(not row or old["version"] == data.version, "Vehicle changed. Refresh before editing.", 409)
    values = data.model_dump(mode="json", exclude={"id", "version"})
    if "subtype" not in data.model_fields_set and data.type == old.get("type"):
        values["subtype"] = old.get("subtype")
    registration = " ".join(data.registration.upper().split())
    registration_key = registration.replace(" ", "").replace("-", "")
    require(registration_key, "Enter a vehicle registration", 400)
    existing = db.scalars(select(VEHICLES).where(
        VEHICLES.organisation_id == org,
        func.replace(func.replace(func.upper(VEHICLES.data["registration"].as_string()), " ", ""), "-", "") == registration_key,
    ))
    require(not any(not row or other.id != row.id for other in existing), "This registration is already in your fleet", 409)
    if data.driverId:
        require(any(driver.id == data.driverId for driver in drivers(db, org)),
                "Select a driver from your organisation", 400)
    for url in set(data.photos) - set(old.get("photos", [])):
        own_upload(db, party, url, "PASSENGER_PHOTO")
    for key, url in data.documents.items():
        if old.get("documents", {}).get(key) != url:
            own_upload(db, party, url, "PASSENGER_DOCUMENT")
    values["registration"] = registration
    require(len(set(data.documents.values())) == len(data.documents), "Upload a separate file for each document", 400)
    review_changed = any(values[key] != old.get(key) for key in REVIEW_FIELDS)
    vehicle = {
        **old, **values, "id": row.id if row else new_id("VH"),
        "version": new_id("REV"), "category": "PASSENGER", "ownerId": old.get("ownerId", party.id),
        "status": "ACTIVE", "createdAt": old.get("createdAt", iso()), "updatedAt": iso(),
    }
    if row and (review_changed or any(values[key] != old.get(key) for key in ("driverId", "availability", "luggageKg", "luggageM3", "luggageDimensions", "location", "serviceArea"))):
        from app.controllers.passenger_charter import reservation_active

        require(not reservation_active(db, row.id), "This vehicle has a confirmed passenger charter", 409)
    if review_changed:
        vehicle.update(verification="PENDING", reviewReason="", reviewedBy=None, reviewedAt=None, published=False)
    elif data.published:
        issue = publication_issue(db, vehicle, org)
        require(not issue, issue or "", 409)
    put(db, "vehicles", vehicle, vehicle["ownerId"])
    return {"id": vehicle["id"], "verification": vehicle["verification"], "published": vehicle["published"]}


def supplier(db, row):
    company = db.get(Organisation, row.organisation_id)
    return company.name if company else "Transporter"
