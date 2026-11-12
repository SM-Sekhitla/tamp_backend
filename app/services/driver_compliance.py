from datetime import date, datetime, timezone

from sqlalchemy import select

from app.api.deps import is_admin
from app.core.errors import require
from app.core.security import iso, new_id
from app.db.models import MODELS, OrganisationMembership, User, UserRole
from app.services.rbac import ROLE_IDS, require_permission

DOCUMENT_TYPES = {
    "IDENTITY_DOCUMENT",
    "DRIVING_LICENCE",
    "PRDP",
    "MEDICAL_CERTIFICATE",
    "EMPLOYMENT_CONTRACT",
}


def _authorize(db, party, organisation_id, driver_id, *, manage=False):
    require_permission(
        db,
        party,
        "organisations.members.manage" if manage else "organisations.read",
    )
    if not is_admin(party):
        membership = db.scalar(
            select(OrganisationMembership.id).where(
                OrganisationMembership.organisation_id == organisation_id,
                OrganisationMembership.user_id == party.id,
                OrganisationMembership.status == "ACTIVE",
            )
        )
        require(membership is not None, "Organisation is not visible to this account", 403)
    driver_membership = db.scalar(
        select(OrganisationMembership.id).where(
            OrganisationMembership.organisation_id == organisation_id,
            OrganisationMembership.user_id == driver_id,
            OrganisationMembership.status == "ACTIVE",
        )
    )
    driver_role = db.scalar(
        select(UserRole.id).where(
            UserRole.organisation_id == organisation_id,
            UserRole.user_id == driver_id,
            UserRole.role_id == ROLE_IDS["DRIVER"],
        )
    )
    require(driver_membership is not None and driver_role is not None, "Driver not found in this organisation", 404)
    driver = db.get(User, driver_id)
    require(driver is not None, "Driver not found", 404)
    return driver


def _status(expires_at):
    if not expires_at:
        return "VALID"
    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).date()
    days = (expiry - datetime.now(timezone.utc).date()).days
    return "EXPIRED" if days < 0 else "EXPIRING" if days <= 30 else "VALID"


def _payload(row):
    return {**row.data, "status": _status(row.data.get("expiresAt"))}


def list_documents(db, party, organisation_id, driver_id):
    _authorize(db, party, organisation_id, driver_id, manage=True)
    return [
        _payload(row)
        for row in db.scalars(
            select(MODELS["driverDocuments"]).where(
                MODELS["driverDocuments"].organisation_id == organisation_id,
                MODELS["driverDocuments"].driver_id == driver_id,
            )
        )
        if row.data.get("status") != "REMOVED"
    ]


def create_document(db, party, organisation_id, driver_id, *, document_type, document_number, issued_at, expires_at, upload):
    driver = _authorize(db, party, organisation_id, driver_id, manage=True)
    require(document_type in DOCUMENT_TYPES, "Select a valid driver document type", 400)
    for field, value in (("issue date", issued_at), ("expiry date", expires_at)):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                require(False, f"Select a valid {field}", 400)
    require(not issued_at or not expires_at or expires_at >= issued_at, "Expiry date cannot be before issue date", 400)
    ident = new_id("DD")
    data = {
        "id": ident,
        "organisationId": organisation_id,
        "driverId": driver_id,
        "driverName": driver.data.get("contactName", "Driver"),
        "documentType": document_type,
        "documentNumber": document_number.strip() or None,
        "issuedAt": issued_at or None,
        "expiresAt": expires_at or None,
        "fileUrl": upload["url"],
        "filename": upload["filename"],
        "contentType": upload["contentType"],
        "size": upload["size"],
        "uploadedAt": iso(),
        "uploadedBy": party.id,
        "status": "VALID",
    }
    model = MODELS["driverDocuments"]
    db.add(model(id=ident, owner_id=driver_id, organisation_id=organisation_id, driver_id=driver_id, data=data))
    db.flush()
    return {**data, "status": _status(expires_at or None)}


def delete_document(db, party, organisation_id, driver_id, document_id):
    _authorize(db, party, organisation_id, driver_id, manage=True)
    row = db.get(MODELS["driverDocuments"], document_id)
    require(row is not None and row.organisation_id == organisation_id and row.driver_id == driver_id, "Driver document not found", 404)
    row.data = {**row.data, "status": "REMOVED", "removedAt": iso(), "removedBy": party.id}
    return {"ok": True}
