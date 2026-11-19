"""Platform review actions for uploaded compliance evidence."""

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from app.core.errors import require
from app.core.security import iso, new_id
from app.db.models import MODELS, User
from app.services.domain import audit
from app.services.rbac import primary_organisation_id, require_permission
from app.services.records import get, put, rows


DOCUMENT_LABELS = {
    "IDENTITY_DOCUMENT": "Identity document",
    "DRIVING_LICENCE": "Driving licence",
    "PRDP": "Professional Driving Permit (PrDP)",
    "MEDICAL_CERTIFICATE": "Medical certificate",
    "EMPLOYMENT_CONTRACT": "Employment contract",
    "VEHICLE_LICENCE": "Vehicle licence disc",
    "ROADWORTHY": "Roadworthy certificate",
    "INSURANCE": "Insurance certificate",
    "CIPC_REGISTRATION": "CIPC registration",
    "TAX_CLEARANCE": "Tax clearance",
    "PROOF_OF_ADDRESS": "Proof of address",
    "BANK_CONFIRMATION": "Bank confirmation",
    "OPERATING_LICENCE": "Operating licence",
}


def _friendly_document_name(document, fallback="Document"):
    """Return a safe, human-readable label for stored document metadata."""
    document = document if isinstance(document, dict) else {}
    category = document.get("documentType") or document.get("category") or document.get("type")
    if category:
        normalized = str(category).strip().upper().replace("-", "_").replace(" ", "_")
        if normalized in DOCUMENT_LABELS:
            return DOCUMENT_LABELS[normalized]
    for field in ("label", "name", "title"):
        value = document.get(field)
        if value and str(value).strip():
            return str(value).strip()
    filename = document.get("filename")
    if filename and str(filename).strip():
        return PurePosixPath(str(filename)).stem.replace("_", " ").replace("-", " ").strip().title()
    return fallback


def _filename_from_document(document):
    document = document if isinstance(document, dict) else {}
    if document.get("filename"):
        return str(document["filename"]).strip()
    url = document.get("url") or document.get("fileUrl")
    if not url:
        return None
    name = PurePosixPath(unquote(urlparse(str(url)).path)).name
    return name or None


def _review_notification(document_name, review_status, *, context=None, filename=None, reason=None):
    outcome = "approved" if review_status == "APPROVED" else "rejected"
    title = f"{document_name} {outcome}"
    subject = f"Your {document_name.lower()}"
    if context:
        subject = f"{document_name} for {context}"
    detail = f"{subject} was {outcome}."
    if filename and filename.lower() != document_name.lower():
        detail += f" File: {filename}."
    if review_status == "REJECTED" and reason:
        detail += f" Reason: {reason}"
    return title, detail


def _notify(db, recipient_id, subject_type, subject_id, title, detail):
    recipient = db.get(User, recipient_id)
    organisation_id = primary_organisation_id(db, recipient) if recipient else None
    ident = new_id("NT")
    db.add(
        MODELS["notifications"](
            id=ident,
            owner_id=recipient_id,
            organisation_id=organisation_id,
            data={
                "id": ident,
                "title": title,
                "detail": detail,
                "at": iso(),
                "unread": True,
                "tone": "warning" if "rejected" in title.lower() else "info",
                "subjectType": subject_type,
                "subjectId": subject_id,
            },
        )
    )
    db.flush()


def handle(action, data, db, request, response, party):
    del request, response
    require_permission(db, party, "users.verify")
    if action == "list":
        result = []
        for row in rows(db, "driverDocuments"):
            if row.data.get("status") == "REMOVED" or row.data.get("reviewStatus") == "APPROVED":
                continue
            result.append(
                {
                    **row.data,
                    "subjectType": "DRIVER",
                    "subjectId": row.data["driverId"],
                    "documentId": row.id,
                    "reviewStatus": row.data.get("reviewStatus", "PENDING"),
                }
            )
        return result
    require(action == "review", "Document action not found", 404)
    values = data.model_dump(mode="json", exclude_none=True)
    review_status = values["status"]
    reason = values.get("reason", "").strip()
    require(review_status != "REJECTED" or reason, "Add a reason when rejecting a document", 400)
    reviewed = {
        "reviewStatus": review_status,
        "reviewedAt": iso(),
        "reviewedBy": party.id,
        "reviewReason": reason or None,
    }
    subject_type = values["subjectType"]

    document_name = "Document"
    document_filename = None
    context = None

    if subject_type == "REGISTRATION":
        user = db.get(User, values["subjectId"])
        require(user is not None, "Applicant not found", 404)
        onboarding = dict(user.onboarding or {})
        documents = list(onboarding.get("documents", []))
        found = False
        reviewed_document = None
        updated = []
        for document in documents:
            if isinstance(document, dict) and (
                document.get("id") == values["documentId"]
                or document.get("url") == values["documentId"]
            ):
                updated.append({**document, **reviewed})
                found = True
                reviewed_document = document
            else:
                updated.append(document)
        require(found, "Registration document not found", 404)
        user.onboarding = {**onboarding, "documents": updated}
        recipient_id, subject_id = user.id, user.id
        document_name = _friendly_document_name(reviewed_document, "Registration document")
        document_filename = _filename_from_document(reviewed_document)
        context = "your registration application"

    elif subject_type == "DRIVER":
        row = get(db, "driverDocuments", values["documentId"], True)
        require(row is not None, "Driver document not found", 404)
        row.data = {**row.data, **reviewed}
        recipient_id = row.owner_id
        subject_id = row.data.get("driverId") or row.owner_id
        document_name = _friendly_document_name(row.data, "Driver document")
        document_filename = _filename_from_document(row.data)
        driver_name = row.data.get("driverName")
        context = f"driver {driver_name}" if driver_name else "your driver profile"

    else:
        truck_row = get(db, "trucks", values["subjectId"], True)
        require(truck_row is not None, "Truck not found", 404)
        document_id = values["documentId"]
        require(
            document_id in truck_row.data.get("documents", [])
            or any(item.get("id") == document_id for item in truck_row.data.get("documentMetadata", [])),
            "Vehicle document not found",
            404,
        )
        reviews = dict(truck_row.data.get("documentReviews", {}))
        reviews[document_id] = reviewed
        truck_row.data = {**truck_row.data, "documentReviews": reviews}
        recipient_id, subject_id = truck_row.owner_id, truck_row.id
        metadata = next(
            (
                item
                for item in truck_row.data.get("documentMetadata", [])
                if item.get("id") == document_id or item.get("url") == document_id
            ),
            None,
        )
        if metadata is None:
            document_urls = truck_row.data.get("documents", [])
            categories = ["VEHICLE_LICENCE", "ROADWORTHY", "INSURANCE"]
            index = document_urls.index(document_id) if document_id in document_urls else -1
            metadata = {
                "url": document_id,
                "category": categories[index] if 0 <= index < len(categories) else "VEHICLE_DOCUMENT",
            }
        document_name = _friendly_document_name(metadata, "Vehicle document")
        document_filename = _filename_from_document(metadata)
        registration = truck_row.data.get("registration")
        context = f"truck {registration}" if registration else "your truck"

    title, detail = _review_notification(
        document_name,
        review_status,
        context=context,
        filename=document_filename,
        reason=reason,
    )
    _notify(db, recipient_id, subject_type, subject_id, title, detail)
    audit(db, party, f"DOCUMENT_{review_status}", subject_type, subject_id, detail)
    return {"ok": True, **reviewed}
