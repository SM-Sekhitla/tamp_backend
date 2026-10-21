from sqlalchemy import delete, select

from app.api.deps import COOKIE, is_admin, party_payload
from app.controllers.auth import find_email
from app.core.errors import require
from app.core.security import new_id, now, token_hash
from app.db.models import Organisation, OrganisationMembership, Session, User, UserRole
from app.services.rbac import ROLE_IDS, require_permission


def _membership(db, user_id, organisation_id):
    return db.scalar(
        select(OrganisationMembership).where(
            OrganisationMembership.user_id == user_id,
            OrganisationMembership.organisation_id == organisation_id,
            OrganisationMembership.status == "ACTIVE",
        )
    )


def _require_access(db, party, organisation_id, permission):
    require_permission(db, party, permission)
    require(
        is_admin(party) or _membership(db, party.id, organisation_id) is not None,
        "Organisation is not visible to this account",
        403,
    )
    organisation = db.get(Organisation, organisation_id)
    require(organisation is not None, "Organisation not found", 404)
    return organisation


def _payload(organisation, membership=None):
    return {
        "id": organisation.id,
        "name": organisation.name,
        "type": organisation.type,
        "verification": organisation.verification,
        "isPrimary": membership.is_primary if membership else False,
        "createdAt": organisation.created_at.isoformat(),
    }


def handle(action, data, db, request, response, party):
    values = data.model_dump(mode="json", exclude_none=True)
    if action == "list":
        require_permission(db, party, "organisations.read")
        if is_admin(party):
            return [_payload(item) for item in db.scalars(select(Organisation))]
        memberships = list(
            db.scalars(
                select(OrganisationMembership).where(
                    OrganisationMembership.user_id == party.id,
                    OrganisationMembership.status == "ACTIVE",
                )
            )
        )
        return [
            _payload(db.get(Organisation, membership.organisation_id), membership)
            for membership in memberships
        ]
    if action == "members":
        organisation = _require_access(
            db, party, values["organisationId"], "organisations.read"
        )
        memberships = list(
            db.scalars(
                select(OrganisationMembership).where(
                    OrganisationMembership.organisation_id == organisation.id,
                    OrganisationMembership.status == "ACTIVE",
                )
            )
        )
        role_rows = list(
            db.scalars(select(UserRole).where(UserRole.organisation_id == organisation.id))
        )
        return [
            {
                **party_payload(db.get(User, membership.user_id)),
                "membershipId": membership.id,
                "isPrimary": membership.is_primary,
                "roleIds": [row.role_id for row in role_rows if row.user_id == membership.user_id],
            }
            for membership in memberships
        ]
    if action == "create":
        require_permission(db, party, "organisations.manage")
        organisation = Organisation(
            id=new_id("ORG"),
            name=values["name"].strip(),
            type=values["type"],
            verification="PENDING",
            data={"createdByUserId": party.id},
            created_at=now(),
        )
        db.add(organisation)
        db.flush()
        membership = OrganisationMembership(
            id=new_id("OM"),
            organisation_id=organisation.id,
            user_id=party.id,
            status="ACTIVE",
            is_primary=False,
            created_at=now(),
        )
        db.add(membership)
        role_id = ROLE_IDS.get(party.data.get("role"), ROLE_IDS["DRIVER"])
        db.add(
            UserRole(
                id=new_id("UR"),
                user_id=party.id,
                role_id=role_id,
                organisation_id=organisation.id,
                assigned_by=party.id,
                created_at=now(),
            )
        )
        return _payload(organisation, membership)
    if action == "switch":
        organisation = _require_access(
            db, party, values["organisationId"], "organisations.read"
        )
        session = db.get(Session, token_hash(request.cookies.get(COOKIE, "")))
        require(session is not None, "Session not found", 401)
        session.active_organisation_id = organisation.id
        db.info["active_organisation_id"] = organisation.id
        return {"ok": True, "activeOrganisationId": organisation.id}
    if action == "update":
        organisation = _require_access(
            db, party, values["organisationId"], "organisations.manage"
        )
        organisation.name = values["name"].strip()
        return _payload(organisation, _membership(db, party.id, organisation.id))
    if action == "setMemberRoles":
        organisation = _require_access(
            db, party, values["organisationId"], "organisations.manage"
        )
        membership = _membership(db, values["userId"], organisation.id)
        require(membership is not None, "Organisation member not found", 404)
        allowed_ids = {ROLE_IDS[role] for role in values["roles"]}
        db.execute(
            delete(UserRole).where(
                UserRole.user_id == values["userId"],
                UserRole.organisation_id == organisation.id,
                UserRole.role_id.not_in(allowed_ids),
            )
        )
        existing = set(
            db.scalars(
                select(UserRole.role_id).where(
                    UserRole.user_id == values["userId"],
                    UserRole.organisation_id == organisation.id,
                )
            )
        )
        for role_id in allowed_ids - existing:
            db.add(UserRole(id=new_id("UR"), user_id=values["userId"], role_id=role_id, organisation_id=organisation.id, assigned_by=party.id, created_at=now()))
        return {"ok": True}
    if action == "removeMember":
        organisation = _require_access(
            db, party, values["organisationId"], "organisations.manage"
        )
        membership = _membership(db, values["userId"], organisation.id)
        require(membership is not None, "Organisation member not found", 404)
        manager_roles = {ROLE_IDS["FREIGHT_OWNER"], ROLE_IDS["TRANSPORTER"]}
        managers = set(
            db.scalars(
                select(UserRole.user_id).where(
                    UserRole.organisation_id == organisation.id,
                    UserRole.role_id.in_(manager_roles),
                    UserRole.user_id != values["userId"],
                )
            )
        )
        require(managers, "The last organisation manager cannot be removed", 409)
        db.execute(delete(UserRole).where(UserRole.user_id == values["userId"], UserRole.organisation_id == organisation.id))
        db.delete(membership)
        return {"ok": True}
    require(action == "addMember", "Unknown organisation action", 404)
    organisation = _require_access(
        db, party, values["organisationId"], "organisations.manage"
    )
    user = find_email(db, values["email"])
    require(user is not None, "Registered user not found", 404)
    membership = _membership(db, user.id, organisation.id)
    if membership is None:
        membership = OrganisationMembership(
            id=new_id("OM"),
            organisation_id=organisation.id,
            user_id=user.id,
            status="ACTIVE",
            is_primary=False,
            created_at=now(),
        )
        db.add(membership)
    role_id = ROLE_IDS[values["role"]]
    assignment = db.scalar(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_id == role_id,
            UserRole.organisation_id == organisation.id,
        )
    )
    if assignment is None:
        db.add(
            UserRole(
                id=new_id("UR"),
                user_id=user.id,
                role_id=role_id,
                organisation_id=organisation.id,
                assigned_by=party.id,
                created_at=now(),
            )
        )
    return {"ok": True, "userId": user.id}
