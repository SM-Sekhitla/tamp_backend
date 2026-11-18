"""Platform review actions for uploaded compliance evidence."""

from app.core.errors import require
from app.core.security import iso, new_id
from app.db.models import MODELS, User
from app.services.domain import audit
from app.services.rbac import primary_organisation_id, require_permission
from app.services.records import get, put, rows


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

    if subject_type == "REGISTRATION":
        user = db.get(User, values["subjectId"])
        require(user is not None, "Applicant not found", 404)
        onboarding = dict(user.onboarding or {})
        documents = list(onboarding.get("documents", []))
        found = False
        updated = []
        for document in documents:
            if isinstance(document, dict) and (
                document.get("id") == values["documentId"]
                or document.get("url") == values["documentId"]
            ):
                updated.append({**document, **reviewed})
                found = True
            else:
                updated.append(document)
        require(found, "Registration document not found", 404)
        user.onboarding = {**onboarding, "documents": updated}
        recipient_id, subject_id = user.id, user.id

    elif subject_type == "DRIVER":
        row = get(db, "driverDocuments", values["documentId"], True)
        require(row is not None, "Driver document not found", 404)
        row.data = {**row.data, **reviewed}
        recipient_id, subject_id = row.owner_id, row.id

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

    title = f"Document {'approved' if review_status == 'APPROVED' else 'rejected'}"
    _notify(db, recipient_id, subject_type, subject_id, title, reason or "Your submitted document was approved.")
    audit(db, party, f"DOCUMENT_{review_status}", subject_type, subject_id, reason or title)
    return {"ok": True, **reviewed}
