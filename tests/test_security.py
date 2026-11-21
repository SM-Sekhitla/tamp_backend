from io import BytesIO

from PIL import Image

from app.core.config import settings
from app.core.security import validate_new_password
from app.services.rbac import UNVERIFIED_ALLOWED_MUTATIONS

from .conftest import async_run, db_call, login, result, rpc


def test_password_policy_uses_eight_character_minimum():
    assert validate_new_password("Good123A")
    assert not validate_new_password("Good12A")
    assert not validate_new_password("alllowercase1")
    assert not validate_new_password("ALLUPPERCASE1")
    assert not validate_new_password("NoNumbers")


def test_pending_registration_can_attach_its_private_documents():
    assert "users.attachRegistrationDocuments" in UNVERIFIED_ALLOWED_MUTATIONS
    assert "parties.attachRegistrationDocuments" in UNVERIFIED_ALLOWED_MUTATIONS


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


def test_document_pdf_with_embedded_features_is_accepted(client, accounts):
    login(client, "owner")
    response = client.post(
        "/api/uploads",
        data={"kind": "KYC"},
        files={
            "file": (
                "unsafe.pdf",
                b"%PDF-1.7\n1 0 obj <</OpenAction <</S /JavaScript /JS (app.alert(1))>>>> endobj",
                "application/pdf",
            )
        },
    )
    assert response.status_code == 200, response.text
    uploaded = response.json()
    assert uploaded["contentType"] == "application/pdf"
    assert client.get(uploaded["url"]).status_code == 200


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
    assert response.headers["permissions-policy"]


def test_admin_cors_preflight_allows_protected_updates(client):
    response = client.options(
        "/api/v1/admin/matching-config",
        headers={
            "Origin": settings.cors_origins[0],
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type,x-tamp-request",
        },
    )
    assert response.status_code == 200
    assert "PUT" in response.headers["access-control-allow-methods"]


def test_password_reset_uses_high_entropy_token(client, accounts):
    reset = result(
        rpc(client, "auth.requestPasswordReset", {"email": "owner@example.com"}, True)
    )
    assert len(reset["devCode"]) >= 32


def test_login_attempts_are_limited_per_account(client, accounts):
    previous = settings.auth_account_attempts
    settings.auth_account_attempts = 2
    try:
        for _ in range(2):
            assert rpc(
                client,
                "auth.login",
                {"email": "owner@example.com", "password": "incorrect-password"},
                True,
            ).status_code == 401
        assert rpc(
            client,
            "auth.login",
            {"email": "owner@example.com", "password": "Testing-password-123"},
            True,
        ).status_code == 429
    finally:
        settings.auth_account_attempts = previous


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
        True,
    )
    assert response.status_code == 403


def test_webhook_outbox_and_worker_retry(client, accounts, monkeypatch, caplog):
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
        with caplog.at_level("INFO", logger="tamp.webhook_worker"):
            assert async_run(tick())
        assert calls[0][2] == hook["secret"]
        completed = [
            record.message
            for record in caplog.records
            if '"event":"webhook_delivery_completed"' in record.message
        ]
        assert completed
        assert '"outcome":"delivered"' in completed[-1]
        assert '"total_ms":' in completed[-1]
        assert '"queue_wait_ms":' in completed[-1]
        assert '"payload_bytes":' in completed[-1]

        def verify_database(db):
            assert rows(db, "deliveries")[0].data["status"] == "DELIVERED"
            assert "encryptedSecret" in rows(db, "webhooks")[0].data

        db_call(verify_database)
        listed = result(rpc(client, "webhooks.list"))
        assert "encryptedSecret" not in listed[0]
    finally:
        settings.webhook_allowed_hosts = previous


def test_local_city_directory_is_available_without_google(client):
    predictions = result(rpc(client, "geocode.autocomplete", {"query": "Johannesburg"}))
    assert predictions[0]["place"]["province"] == "GP"
    assert result(rpc(client, "geocode.details", {"placeId": predictions[0]["id"]}))["lat"] < 0
