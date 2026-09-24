import logging

from app.api.deps import party_payload
from app.core.config import settings
from app.core.security import verify_password
from app.db.models import Party
from app.db.session import SessionLocal
from app.services.initial_users import ensure_platform_super_user


def test_bootstrap_super_user_is_created_and_password_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(settings, "bootstrap_secret", "test-bootstrap-secret")
    monkeypatch.setattr(settings, "default_platform_admin_username", "platform-admin")
    monkeypatch.setattr(settings, "default_platform_admin_email", "platform-admin@example.com")
    monkeypatch.setattr(settings, "default_platform_admin_password", None)
    monkeypatch.setattr(
        "app.services.initial_users._generate_password",
        lambda: "Temporary-password-for-test",
    )

    with caplog.at_level(logging.WARNING, logger="tamp.bootstrap"):
        first_id = ensure_platform_super_user()
        second_id = ensure_platform_super_user()

    assert first_id == second_id
    messages = [record.getMessage() for record in caplog.records]
    assert sum("temporary_password=Temporary-password-for-test" in m for m in messages) == 1
    assert sum("Platform system_super_user ready" in m for m in messages) == 1

    with SessionLocal() as db:
        party = db.get(Party, first_id)
        assert party.data["role"] == "system_super_user"
        assert party.data["username"] == "platform-admin"
        assert party.data["temporaryPassword"] is True
        assert verify_password("Temporary-password-for-test", party.password_hash)
        assert party_payload(party)["role"] == "ADMIN"
        assert party_payload(party)["systemRole"] == "system_super_user"
