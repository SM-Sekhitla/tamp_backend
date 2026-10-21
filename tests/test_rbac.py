from sqlalchemy import select

from app.db.models import OrganisationMembership, UserRole
from app.services.rbac import permission_codes

from .conftest import db_call, login, result, rpc


def test_login_exposes_organisation_and_effective_permissions(client, accounts):
    user = login(client, "owner")
    assert user["organisations"][0]["name"] == "owner Co"
    assert user["organisations"][0]["isPrimary"] is True
    assert "loads.manage" in user["permissions"]
    assert "users.verify" not in user["permissions"]

    def verify(db):
        membership = db.scalar(
            select(OrganisationMembership).where(
                OrganisationMembership.user_id == accounts["owner"]["id"]
            )
        )
        assignment = db.scalar(
            select(UserRole).where(UserRole.user_id == accounts["owner"]["id"])
        )
        assert membership is not None and assignment is not None

    db_call(verify)


def test_sensitive_procedures_use_rbac_permissions(client, accounts):
    login(client, "owner")
    denied = rpc(
        client,
        "parties.setVerification",
        {"partyId": accounts["other"]["id"], "status": "REJECTED"},
        True,
    )
    assert denied.status_code == 403

    result(rpc(client, "auth.logout", {}, True))
    admin = login(client, "admin")
    assert "platform.admin" in admin["permissions"]
    result(
        rpc(
            client,
            "parties.setVerification",
            {"partyId": accounts["other"]["id"], "status": "VERIFIED"},
            True,
        )
    )

    def verify(db):
        from app.db.models import User

        administrator = db.get(User, accounts["admin"]["id"])
        assert "users.verify" in permission_codes(db, administrator)

    db_call(verify)
