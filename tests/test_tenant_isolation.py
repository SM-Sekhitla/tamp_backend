from sqlalchemy import select

from app.db.models import OrganisationMembership

from .conftest import db_call, login, rpc


def _primary_organisation(user_id):
    def find(db):
        return db.scalar(
            select(OrganisationMembership.organisation_id).where(
                OrganisationMembership.user_id == user_id,
                OrganisationMembership.is_primary.is_(True),
            )
        )

    return db_call(find)


def test_organisation_members_and_roles_are_tenant_scoped(client, accounts):
    login(client, "owner")
    other_organisation_id = _primary_organisation(accounts["other"]["id"])

    members = rpc(
        client,
        "organisations.members",
        {"organisationId": other_organisation_id},
    )
    assert members.status_code == 403

    roles = rpc(
        client,
        "organisations.setMemberRoles",
        {
            "organisationId": other_organisation_id,
            "userId": accounts["other"]["id"],
            "roles": ["CARGO_OPERATOR"],
        },
        True,
    )
    assert roles.status_code == 403


def test_driver_documents_cannot_cross_organisation_boundaries(client, accounts):
    login(client, "carrier")
    other_organisation_id = _primary_organisation(accounts["owner"]["id"])

    response = client.get(
        f"/api/v1/organisations/{other_organisation_id}/drivers/{accounts['driver']['id']}/documents"
    )
    assert response.status_code == 403

