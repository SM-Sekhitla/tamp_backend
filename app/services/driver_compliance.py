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
REQUIRED_DISPATCH_DOCUMENTS = {"DRIVING_LICENCE", "PRDP"}
MIN_DOCUMENT_DATE = date(1900, 1, 1)
MAX_DOCUMENT_DATE = date(2100, 12, 31)
REGISTRATION_DOCUMENT_TYPES = {
    "Identity document": "IDENTITY_DOCUMENT",
    "South African ID / passport": "IDENTITY_DOCUMENT",
    "Driving licence": "DRIVING_LICENCE",
    "Driver's licence (front & back)": "DRIVING_LICENCE",
    "Professional Driving Permit (PrDP)": "PRDP",
    "Professional Driving Permit": "PRDP",
    "Medical certificate": "MEDICAL_CERTIFICATE",
    "Employment contract": "EMPLOYMENT_CONTRACT",
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
    data = row.data
    return {
        **data,
        "url": data.get("url") or data.get("fileUrl"),
        "category": data.get("category") or "DRIVER_COMPLIANCE",
        "reviewStatus": data.get("reviewStatus") or "PENDING",
        "status": _status(data.get("expiresAt")),
    }


def list_documents(db, party, organisation_id, driver_id):
    driver = _authorize(db, party, organisation_id, driver_id, manage=True)
    sync_registration_documents(db, driver, organisation_id)
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


def sync_registration_documents(db, driver, organisation_id=None, documents=None):
    """Mirror driver registration evidence into the organisation personnel file.

    Registration review and transporter compliance used to keep independent
    copies. This idempotent bridge makes the uploaded file and its review state
    authoritative in both views without duplicating the physical upload.
    """
    documents = documents if documents is not None else (driver.onboarding or {}).get("documents", [])
    if not documents:
        return 0
    if organisation_id is None:
        organisation_ids = list(
            db.scalars(
                select(OrganisationMembership.organisation_id).where(
                    OrganisationMembership.user_id == driver.id,
                    OrganisationMembership.status == "ACTIVE",
                )
            )
        )
    else:
        organisation_ids = [organisation_id]
    model = MODELS["driverDocuments"]
    changed = 0
    for target_organisation_id in organisation_ids:
        has_driver_role = db.scalar(
            select(UserRole.id).where(
                UserRole.organisation_id == target_organisation_id,
                UserRole.user_id == driver.id,
                UserRole.role_id == ROLE_IDS["DRIVER"],
            )
        )
        if not has_driver_role:
            continue
        existing_rows = list(
            db.scalars(
                select(model).where(
                    model.organisation_id == target_organisation_id,
                    model.driver_id == driver.id,
                )
            )
        )
        for document in documents:
            if not isinstance(document, dict) or not document.get("url"):
                continue
            document_type = REGISTRATION_DOCUMENT_TYPES.get(str(document.get("label", "")).strip())
            if not document_type:
                continue
            existing = next(
                (
                    row
                    for row in existing_rows
                    if row.data.get("documentType") == document_type
                    and row.data.get("source") == "REGISTRATION"
                    and row.data.get("status") != "REMOVED"
                ),
                None,
            )
            ident = existing.id if existing else new_id("DD")
            data = {
                **(existing.data if existing else {}),
                "id": ident,
                "organisationId": target_organisation_id,
                "driverId": driver.id,
                "driverName": driver.data.get("contactName", "Driver"),
                "documentType": document_type,
                "documentNumber": document.get("documentNumber"),
                "issuedAt": document.get("issuedAt"),
                "expiresAt": document.get("expiresAt"),
                "fileUrl": document["url"],
                "url": document["url"],
                "category": "DRIVER_COMPLIANCE",
                "filename": document.get("name") or document.get("filename") or "Driver document",
                "contentType": document.get("contentType"),
                "size": document.get("size"),
                "uploadedAt": document.get("uploadedAt") or iso(),
                "uploadedBy": document.get("uploadedBy") or driver.id,
                "status": "VALID",
                "reviewStatus": document.get("reviewStatus") or "PENDING",
                "reviewReason": document.get("reviewReason"),
                "reviewedAt": document.get("reviewedAt"),
                "reviewedBy": document.get("reviewedBy"),
                "source": "REGISTRATION",
                "registrationDocumentId": document.get("id"),
            }
            if existing:
                if existing.data != data:
                    existing.data = data
                    changed += 1
            else:
                row = model(
                    id=ident,
                    owner_id=driver.id,
                    organisation_id=target_organisation_id,
                    driver_id=driver.id,
                    data=data,
                )
                db.add(row)
                existing_rows.append(row)
                changed += 1
    if changed:
        driver.data = {**driver.data, "requiresDriverCompliance": True}
        db.flush()
    return changed


def create_document(db, party, organisation_id, driver_id, *, document_type, document_number, issued_at, expires_at, upload):
    driver = _authorize(db, party, organisation_id, driver_id, manage=True)
    require(document_type in DOCUMENT_TYPES, "Select a valid driver document type", 400)
    parsed_dates = {}
    for field, value in (("issue date", issued_at), ("expiry date", expires_at)):
        if value:
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                require(False, f"Select a valid {field}", 400)
            require(MIN_DOCUMENT_DATE <= parsed <= MAX_DOCUMENT_DATE, f"Select a valid {field} between 1900/01/01 and 2100/12/31", 400)
            parsed_dates[field] = parsed
    require(
        not issued_at
        or not expires_at
        or parsed_dates["expiry date"] >= parsed_dates["issue date"],
        "Expiry date cannot be before issue date",
        400,
    )
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
        "url": upload["url"],
        "category": "DRIVER_COMPLIANCE",
        "filename": upload["filename"],
        "contentType": upload["contentType"],
        "size": upload["size"],
        "uploadedAt": iso(),
        "uploadedBy": party.id,
        "status": "VALID",
        "reviewStatus": "PENDING",
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


def require_dispatch_compliance(db, driver, organisation_id):
    """Block dispatch when a managed driver's required evidence is not valid."""

    records = [
        row.data
        for row in db.scalars(
            select(MODELS["driverDocuments"]).where(
                MODELS["driverDocuments"].organisation_id == organisation_id,
                MODELS["driverDocuments"].driver_id == driver.id,
            )
        )
        if row.data.get("status") != "REMOVED"
    ]
    requires_documents = driver.data.get("requiresDriverCompliance", False) or bool(records)
    if not requires_documents:
        return
    by_type = {item.get("documentType"): item for item in records}
    missing = sorted(REQUIRED_DISPATCH_DOCUMENTS - set(by_type))
    require(not missing, f"Driver compliance documents missing: {', '.join(missing)}", 409)
    for document_type in REQUIRED_DISPATCH_DOCUMENTS:
        document = by_type[document_type]
        require(
            document.get("reviewStatus") == "APPROVED",
            f"Driver {document_type.lower().replace('_', ' ')} is awaiting approval",
            409,
        )
        require(
            _status(document.get("expiresAt")) != "EXPIRED",
            f"Driver {document_type.lower().replace('_', ' ')} has expired",
            409,
        )
