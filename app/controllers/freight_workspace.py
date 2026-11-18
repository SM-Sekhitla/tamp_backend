from app.core.errors import require
from app.core.security import iso, new_id
from app.services.domain import audit
from app.services.rbac import require_permission
from app.services.records import get, put, rows


KINDS = {
    "CUSTOMER": ("freightCustomers", "FC"),
    "LOCATION": ("savedLocations", "SL"),
    "TEMPLATE": ("loadTemplates", "LT"),
}


def _organisation_id(db):
    organisation_id = db.info.get("active_organisation_id")
    require(organisation_id, "Select an organisation first", 409)
    return organisation_id


def _scoped(db, collection):
    organisation_id = _organisation_id(db)
    return [
        row.data
        for row in rows(db, collection)
        if row.organisation_id == organisation_id and row.data.get("active", True)
    ]


def _save(db, party, collection, prefix, values):
    require_permission(db, party, "loads.manage")
    require(party.data.get("role") == "FREIGHT_OWNER", "Freight Owner access is required", 403)
    organisation_id = _organisation_id(db)
    ident = values.pop("id", None) or new_id(prefix)
    existing = get(db, collection, ident, True)
    if existing:
        require(existing.organisation_id == organisation_id, "Record not found", 404)
        created_at = existing.data.get("createdAt", iso())
    else:
        created_at = iso()
    record = {
        **values,
        "id": ident,
        "createdAt": created_at,
        "updatedAt": iso(),
        "createdBy": existing.data.get("createdBy", party.id) if existing else party.id,
    }
    put(db, collection, record, party.id)
    audit(db, party, f"{prefix}_SAVED", collection, ident, f"Saved {values.get('name', ident)}")
    return record


def handle(action, data, db, request, response, party):
    require_permission(db, party, "loads.read" if action == "list" else "loads.manage")
    require(party.data.get("role") == "FREIGHT_OWNER", "Freight Owner access is required", 403)
    if action == "list":
        return {
            "customers": _scoped(db, "freightCustomers"),
            "locations": _scoped(db, "savedLocations"),
            "templates": _scoped(db, "loadTemplates"),
        }
    values = data.model_dump(mode="json", exclude_none=True)
    if action == "saveCustomer":
        return _save(db, party, "freightCustomers", "FC", values)
    if action == "saveLocation":
        customer_id = values.get("customerId")
        if customer_id:
            customer = get(db, "freightCustomers", customer_id)
            require(customer and customer.organisation_id == _organisation_id(db), "Customer not found", 404)
        return _save(db, party, "savedLocations", "SL", values)
    if action == "saveTemplate":
        for key, collection in (("customerId", "freightCustomers"), ("originLocationId", "savedLocations"), ("destinationLocationId", "savedLocations")):
            ident = values.get(key)
            if ident:
                row = get(db, collection, ident)
                require(row and row.organisation_id == _organisation_id(db), f"{key} not found", 404)
        return _save(db, party, "loadTemplates", "LT", values)
    if action == "delete":
        collection, _ = KINDS[values["kind"]]
        row = get(db, collection, values["id"], True)
        require(row and row.organisation_id == _organisation_id(db), "Record not found", 404)
        row.data = {**row.data, "active": False, "updatedAt": iso(), "archivedAt": iso()}
        audit(db, party, "FREIGHT_WORKSPACE_ARCHIVED", collection, values["id"], "Archived freight workspace record")
        return {"ok": True}
    require(False, "Unknown freight workspace action", 404)
