from datetime import date, timedelta

from .conftest import db_call, login, result, rpc


def _carrier_org(client):
    return result(rpc(client, "organisations.list"))[0]["id"]


def test_driver_document_folder_is_tenant_scoped(client, accounts):
    login(client, "carrier")
    organisation_id = _carrier_org(client)
    driver_id = accounts["driver"]["id"]
    expiry = (date.today() + timedelta(days=20)).isoformat()

    response = client.post(
        f"/api/v1/organisations/{organisation_id}/drivers/{driver_id}/documents",
        data={
            "document_type": "DRIVING_LICENCE",
            "document_number": "DL-1234",
            "issued_at": date.today().isoformat(),
            "expires_at": expiry,
        },
        files={"file": ("licence.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["status"] == "EXPIRING"
    assert document["driverId"] == driver_id

    listing = client.get(
        f"/api/v1/organisations/{organisation_id}/drivers/{driver_id}/documents"
    )
    assert listing.status_code == 200
    assert listing.json()[0]["documentNumber"] == "DL-1234"

    login(client, "driver")
    forbidden = client.get(
        f"/api/v1/organisations/{organisation_id}/drivers/{driver_id}/documents"
    )
    assert forbidden.status_code == 403


def test_driver_expiry_alert_targets_carrier_managers(client, accounts):
    login(client, "carrier")
    organisation_id = _carrier_org(client)
    driver_id = accounts["driver"]["id"]
    client.post(
        f"/api/v1/organisations/{organisation_id}/drivers/{driver_id}/documents",
        data={
            "document_type": "PRDP",
            "document_number": "PRDP-9",
            "issued_at": "",
            "expires_at": (date.today() + timedelta(days=5)).isoformat(),
        },
        files={"file": ("prdp.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )

    from app.services.notifications import create_driver_document_expiry_notifications

    created = db_call(create_driver_document_expiry_notifications)
    assert created >= 1
    notices = result(rpc(client, "notifications.list", {"limit": 20}))
    assert any(item["subjectType"] == "DRIVER_DOCUMENT" for item in notices)
