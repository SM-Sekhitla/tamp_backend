import pytest

from app.core.security import token_hash
from app.db.models import Party, Session
from app.db.session import SessionLocal

from .conftest import login, result, rpc


def test_registration_verification_login_and_revocation(client):
    code = result(rpc(client, "email.sendCode", {"email": "new@example.com"}, True))["devCode"]
    payload = dict(
        email="new@example.com",
        password="Strong-password-123",
        code=code,
        userType="CARGO_OWNER",
        firstName="New",
        lastName="Owner",
        phone="0821234567",
        province="GP",
    )
    ident = result(rpc(client, "parties.register", payload, True))["id"]
    assert (
        rpc(
            client, "parties.register", {**payload, "email": "second@example.com"}, True
        ).status_code
        == 400
    )
    r = rpc(
        client, "auth.login", {"email": payload["email"], "password": payload["password"]}, True
    )
    party = result(r)
    assert "HttpOnly" in r.headers["set-cookie"]
    assert party["id"] == ident and party["role"] == "FREIGHT_OWNER"
    with SessionLocal() as db:
        assert db.get(Party, ident).password_hash.startswith("$argon2")
        assert db.get(Session, token_hash(client.cookies["tamp_session"]))
    assert result(rpc(client, "auth.me"))["id"] == ident
    result(rpc(client, "auth.logout", {}, True))
    assert result(rpc(client, "auth.me")) is None


def test_csrf_roles_methods_and_input_validation(client, accounts):
    login(client, "owner")
    assert (
        client.post(
            "/api/trpc/auth.logout?batch=1",
            json={"0": {"json": {}}},
            headers={"X-TAMP-Request": ""},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/trpc/auth.logout?batch=1",
            json={"0": {"json": {}}},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert (
        rpc(
            client,
            "parties.setVerification",
            {"partyId": accounts["other"]["id"], "status": "VERIFIED"},
            True,
        ).status_code
        == 403
    )
    assert rpc(client, "auth.updateProfile", {"role": "ADMIN"}, True).status_code == 400
    assert rpc(client, "auth.logout").status_code == 405
    assert rpc(client, "unknown.operation").status_code == 404
    assert client.get("/openapi.json").status_code == 200


def test_password_reset_invalidates_all_sessions(client, accounts):
    login(client, "owner")
    cookie = client.cookies["tamp_session"]
    code = result(rpc(client, "auth.requestPasswordReset", {"email": "owner@example.com"}, True))[
        "devCode"
    ]
    result(
        rpc(
            client,
            "auth.resetPassword",
            {"email": "owner@example.com", "code": code, "newPassword": "Replacement-password-123"},
            True,
        )
    )
    assert result(rpc(client, "auth.me")) is None
    assert (
        rpc(
            client,
            "auth.login",
            {"email": "owner@example.com", "password": "Testing-password-123"},
            True,
        ).status_code
        == 401
    )
    result(
        rpc(
            client,
            "auth.login",
            {"email": "owner@example.com", "password": "Replacement-password-123"},
            True,
        )
    )
    assert client.cookies["tamp_session"] != cookie


def test_otp_attempts_persist(client):
    code = result(rpc(client, "email.sendCode", {"email": "new@example.com"}, True))["devCode"]
    data = dict(
        email="new@example.com",
        password="Strong-password-123",
        code="999999" if code != "999999" else "888888",
        userType="CARGO_OWNER",
        firstName="New",
        lastName="Owner",
        phone="0821234567",
        province="GP",
    )
    for _ in range(5):
        assert rpc(client, "parties.register", data, True).status_code == 400
    assert rpc(client, "parties.register", {**data, "code": code}, True).status_code == 400


def test_batch_and_direct_api(client, accounts):
    login(client, "owner")
    r = client.get("/api/trpc/auth.me,notifications.unreadCount?batch=1")
    assert r.status_code == 200 and len(r.json()) == 2
    r = client.post("/api/v1/auth/updateProfile", json={"contactName": "Updated"})
    assert r.status_code == 200 and r.json()["contactName"] == "Updated"


def test_bootstrap_local_email_is_login_only():
    from pydantic import ValidationError

    from app.schemas.auth import Login, Register

    Login(email="tamp-admin@tamp.local", password="password")
    with pytest.raises(ValidationError):
        Register(
            email="new-user@tamp.local",
            password="Strong-password-123",
            code="123456",
            userType="CARGO_OWNER",
            firstName="New",
            lastName="User",
            phone="0821234567",
            province="GP",
        )
