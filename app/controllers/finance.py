from app.api.deps import is_admin
from app.core.errors import require
from app.core.security import iso, new_id
from app.services.domain import audit, lock_domain
from app.services.records import needed, put


def _notify(db, party_id, invoice, title):
    put(db, "notifications", {"id": new_id("NT"), "title": title, "detail": invoice["invoiceNumber"], "at": iso(), "unread": True, "tone": "info", "subjectType": "INVOICE", "subjectId": invoice["id"]}, party_id)


def handle(action, data, db, request, response, party):
    require(action == "invoiceAction", "Unknown finance action", 404)
    require(is_admin(party), "Administrator access is required", 403)
    lock_domain(db)
    values = data.model_dump(mode="json", exclude_none=True)
    invoice = needed(db, "invoices", values["invoiceId"], True).data
    operation = values["action"]
    reference = values["reference"].strip()

    if operation == "RECORD_PAYMENT":
        require(invoice["status"] == "ISSUED", "Only an issued invoice can be paid", 409)
        invoice = {**invoice, "status": "PAID", "paidAt": iso(), "paymentReference": reference}
        kind, amount, recipient = "PAYMENT", invoice["total"], invoice["ownerId"]
        title = "Payment recorded"
    elif operation == "RELEASE_PAYOUT":
        require(invoice["status"] == "PAID" and invoice["payoutStatus"] == "PENDING", "Invoice must be paid before payout", 409)
        invoice = {**invoice, "status": "SETTLED", "payoutStatus": "PAID", "paidOutAt": iso(), "payoutReference": reference}
        kind, amount, recipient = "PAYOUT", invoice["carrierPayout"], invoice["carrierId"]
        title = "Carrier payout released"
    else:
        require(invoice["status"] == "ISSUED", "Only an issued invoice can be voided", 409)
        invoice = {**invoice, "status": "VOID", "voidedAt": iso(), "voidReference": reference}
        kind, amount, recipient = "VOID", 0, invoice["ownerId"]
        title = "Invoice voided"

    put(db, "invoices", invoice)
    transaction = {"id": new_id("TX"), "invoiceId": invoice["id"], "type": kind, "amount": amount, "currency": invoice["currency"], "status": "SUCCEEDED", "reference": reference, "note": values.get("note"), "createdAt": iso(), "recordedById": party.id}
    put(db, "transactions", transaction, party.id)
    audit(db, party, f"INVOICE_{operation}", "INVOICE", invoice["id"], f"{title}: {reference}")
    _notify(db, recipient, invoice, title)
    return {"ok": True, "invoice": invoice, "transaction": transaction}
