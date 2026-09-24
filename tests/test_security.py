from io import BytesIO

from PIL import Image

from app.core.config import settings

from .conftest import login, result, rpc


def test_upload_validation_and_private_download(client, accounts):
    login(client, "owner")
    assert (
        client.post(
            "/api/uploads",
            data={"kind": "AVATAR"},
            files={"file": ("bad.svg", b'<svg onload="alert(1)"/>', "image/svg+xml")},
        ).status_code
        == 400
    )
    content = BytesIO()
    Image.new("RGB", (5, 5)).save(content, "PNG")
    response = client.post(
        "/api/uploads",
        data={"kind": "AVATAR"},
        files={"file": ("photo.png", content.getvalue(), "image/png")},
    )
    assert response.status_code == 200, response.text
    file = response.json()
    assert client.get(file["url"]).status_code == 200
    result(rpc(client, "auth.updateProfile", {"avatarUrl": file["url"]}, True))
    login(client, "other")
    assert client.get(file["url"]).status_code == 403
    assert rpc(client, "auth.updateProfile", {"avatarUrl": file["url"]}, True).status_code == 403


def test_rate_limit_and_security_headers(client, accounts):
    previous = settings.auth_rate_limit
    settings.auth_rate_limit = 2
    try:
        for _ in range(2):
            rpc(client, "auth.login", {"email": "absent@example.com", "password": "bad"}, True)
        assert (
            rpc(
                client, "auth.login", {"email": "absent@example.com", "password": "bad"}, True
            ).status_code
            == 429
        )
    finally:
        settings.auth_rate_limit = previous
    response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"]


def test_webhook_ssrf_and_permissions(client, accounts):
    login(client, "owner")
    assert (
        rpc(
            client, "webhooks.create", {"url": "https://example.com", "events": ["*"]}, True
        ).status_code
        == 403
    )
    login(client, "admin")
    for url in ["http://127.0.0.1", "https://169.254.169.254", "https://unapproved.example"]:
        assert (
            rpc(client, "webhooks.create", {"url": url, "events": ["*"]}, True).status_code == 400
        )


def test_production_config_rejects_insecure_defaults():
    import pytest

    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(environment="production", _env_file=None)


def test_masked_truck_does_not_accept_forged_scoring_inputs(client, accounts):
    from .test_workflow import fixtures, sync

    load, truck, _ = fixtures(accounts)
    login(client, "carrier")
    result(sync(client, trucks=[{**truck, "status": "OFFLINE"}]))
    login(client, "owner")
    response = rpc(
        client,
        "matching.score",
        {"load": load, "trucks": [truck], "operators": [], "owner": accounts["owner"]},
    )
    assert response.status_code == 403


def test_webhook_outbox_and_worker_retry(client, accounts, monkeypatch):
    from app.db.session import SessionLocal
    from app.services.records import rows
    from app.services.webhook_worker import tick

    previous = settings.webhook_allowed_hosts
    settings.webhook_allowed_hosts = ["receiver.example"]
    try:
        login(client, "admin")
        hook = result(
            rpc(
                client,
                "webhooks.create",
                {"url": "https://receiver.example/hooks", "events": ["VERIFICATION_CHANGED"]},
                True,
            )
        )
        result(
            rpc(
                client,
                "parties.setVerification",
                {"partyId": accounts["owner"]["id"], "status": "VERIFIED"},
                True,
            )
        )
        calls = []

        def delivery(url, body, secret, ident):
            calls.append((url, body, secret, ident))
            return 204

        monkeypatch.setattr("app.services.webhook_worker.deliver", delivery)
        assert tick()
        assert calls[0][2] == hook["secret"]
        with SessionLocal() as db:
            assert rows(db, "deliveries")[0].data["status"] == "DELIVERED"
            assert "encryptedSecret" in rows(db, "webhooks")[0].data
        listed = result(rpc(client, "webhooks.list"))
        assert "encryptedSecret" not in listed[0]
    finally:
        settings.webhook_allowed_hosts = previous


def test_local_city_directory_is_available_without_google(client):
    predictions = result(rpc(client, "geocode.autocomplete", {"query": "Johannesburg"}))
    assert predictions[0]["place"]["province"] == "GP"
    assert result(rpc(client, "geocode.details", {"placeId": predictions[0]["id"]}))["lat"] < 0
