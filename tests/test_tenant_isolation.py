from sqlalchemy import select

from app.db.models import OrganisationMembership

from .conftest import db_call, login, result, rpc


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


def test_freight_workspace_records_are_organisation_scoped(client, accounts):
    login(client, "owner")
    customer = result(
        rpc(
            client,
            "freightWorkspace.saveCustomer",
            {"name": "Owner Customer", "active": True},
            True,
        )
    )
    assert customer["name"] == "Owner Customer"

    login(client, "other")
    workspace = result(rpc(client, "freightWorkspace.list"))
    assert workspace["customers"] == []

    linked_location = rpc(
        client,
        "freightWorkspace.saveLocation",
        {
            "name": "Cross-tenant location",
            "customerId": customer["id"],
            "place": {
                "label": "1 Main Road, Pretoria",
                "province": "Gauteng",
                "lat": -25.746,
                "lng": 28.188,
            },
            "active": True,
        },
        True,
    )
    assert linked_location.status_code == 404

    archive = rpc(
        client,
        "freightWorkspace.delete",
        {"kind": "CUSTOMER", "id": customer["id"]},
        True,
    )
    assert archive.status_code == 404

