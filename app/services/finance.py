from datetime import timedelta

from app.core.config import settings
from app.core.security import new_id, now
from app.services.records import put, rows


def _money(value):
    return round(float(value) + 1e-9, 2)


def ensure_invoice_for_trip(db, trip, load, truck, match):
    existing = next((row.data for row in rows(db, "invoices") if row.data["tripId"] == trip["id"]), None)
    if existing:
        return existing

    rate = match.get("agreedRate") or load.get("targetRate") or {"amount": 0, "vat": "incl"}
    amount = _money(rate["amount"])
    if rate.get("vat") == "excl":
        subtotal = amount
        vat_amount = _money(subtotal * 0.15)
        total = _money(subtotal + vat_amount)
    else:
        total = amount
        subtotal = _money(total / 1.15)
        vat_amount = _money(total - subtotal)
    platform_fee = _money(subtotal * settings.platform_commission_rate)
    issued_at = now()
    invoice = {
        "id": new_id("IV"),
        "invoiceNumber": f"TAMP-{issued_at:%Y%m%d}-{trip['id'][-6:].upper()}",
        "tripId": trip["id"],
        "loadId": load["id"],
        "matchId": match["id"],
        "ownerId": load["ownerId"],
        "carrierId": truck["transporterId"],
        "currency": "ZAR",
        "subtotal": subtotal,
        "vatRate": 0.15,
        "vatAmount": vat_amount,
        "total": total,
        "platformFeeRate": settings.platform_commission_rate,
        "platformFee": platform_fee,
        "carrierPayout": _money(total - platform_fee),
        "status": "ISSUED",
        "payoutStatus": "PENDING",
        "issuedAt": issued_at.isoformat(),
        "dueAt": (issued_at + timedelta(days=settings.invoice_due_days)).isoformat(),
        "paidAt": None,
        "paidOutAt": None,
        "lineItems": [{"description": f"Freight service {load['origin']['label']} to {load['destination']['label']}", "quantity": 1, "unitAmount": subtotal, "amount": subtotal}],
    }
    put(db, "invoices", invoice, load["ownerId"])
    return invoice
