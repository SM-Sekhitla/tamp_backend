from sqlalchemy import select

from app.core.errors import require
from app.core.security import new_id, now
from app.db.models import (
    Organisation,
    OrganisationMembership,
    Permission,
    Role,
    RolePermission,
    UserRole,
)

ROLE_IDS = {
    "system_super_user": "ROLE-SYSTEM-SUPER-USER",
    "ADMIN": "ROLE-PLATFORM-ADMIN",
    "FREIGHT_OWNER": "ROLE-FREIGHT-OWNER",
    "TRANSPORTER": "ROLE-TRANSPORTER",
    "DRIVER": "ROLE-DRIVER",
}

PERMISSIONS = {
    "platform.admin": "Full platform administration",
    "users.read": "View users",
    "users.verify": "Verify and reject users",
    "organisations.read": "View organisations and memberships",
    "organisations.manage": "Manage organisations and memberships",
    "loads.read": "View permitted loads",
    "loads.manage": "Create and manage freight loads",
    "trucks.read": "View permitted trucks",
    "trucks.manage": "Create and manage fleet vehicles",
    "matches.read": "View match suggestions and offers",
    "matches.manage": "Negotiate and accept matches",
    "trips.read": "View assigned trips",
    "trips.manage": "Advance and manage trips",
    "dispatch.manage": "Assign drivers to trips",
    "dispatch.respond": "Respond to driver assignments",
    "incidents.report": "Report trip incidents",
    "incidents.manage": "Manage operational incidents",
    "finance.read": "View permitted invoices and transactions",
    "finance.manage": "Record settlement actions",
    "messages.read": "View permitted trip conversations",
    "messages.send": "Send trip messages",
    "notifications.read": "View and acknowledge notifications",
    "tracking.read": "View permitted live vehicle positions",
    "tracking.publish": "Publish vehicle positions",
    "webhooks.manage": "Manage outbound webhooks",
    "audit.read": "View audit records",
    "system.logs.read": "Browse backend logs",
    "maps.use": "Use geocoding and routing",
}

ROLE_DEFINITIONS = {
    "system_super_user": {
        "name": "System Super User",
        "scope": "PLATFORM",
        "permissions": set(PERMISSIONS),
    },
    "ADMIN": {
        "name": "Platform Administrator",
        "scope": "PLATFORM",
        "permissions": set(PERMISSIONS),
    },
    "FREIGHT_OWNER": {
        "name": "Freight Owner",
        "scope": "ORGANISATION",
        "permissions": {
            "organisations.read", "organisations.manage", "loads.read", "loads.manage", "trucks.read",
            "matches.read", "matches.manage", "trips.read", "trips.manage", "incidents.report",
            "finance.read", "messages.read", "messages.send", "notifications.read",
            "tracking.read", "maps.use",
        },
    },
    "TRANSPORTER": {
        "name": "Transporter",
        "scope": "ORGANISATION",
        "permissions": {
            "organisations.read", "organisations.manage", "loads.read", "trucks.read", "trucks.manage",
            "matches.read", "matches.manage", "trips.read", "trips.manage",
            "dispatch.manage", "incidents.report", "finance.read", "messages.read",
            "messages.send", "notifications.read", "tracking.read", "tracking.publish",
            "maps.use",
        },
    },
    "DRIVER": {
        "name": "Driver",
        "scope": "ORGANISATION",
        "permissions": {
            "organisations.read", "trucks.read", "trips.read", "trips.manage",
            "dispatch.respond", "incidents.report", "messages.read", "messages.send",
            "notifications.read", "tracking.read", "tracking.publish", "maps.use",
        },
    },
}


def seed_rbac(db):
    for code, description in PERMISSIONS.items():
        if db.get(Permission, code) is None:
            db.add(Permission(code=code, description=description))
    db.flush()
    for legacy_role, definition in ROLE_DEFINITIONS.items():
        role_id = ROLE_IDS[legacy_role]
        role = db.get(Role, role_id)
        if role is None:
            role = Role(
                id=role_id,
                name=definition["name"],
                scope=definition["scope"],
                description=f"Built-in {definition['name']} role",
                system=True,
            )
            db.add(role)
    db.flush()
    for legacy_role, definition in ROLE_DEFINITIONS.items():
        role_id = ROLE_IDS[legacy_role]
        existing = set(
            db.scalars(
                select(RolePermission.permission_code).where(RolePermission.role_id == role_id)
            )
        )
        for code in definition["permissions"] - existing:
            db.add(RolePermission(role_id=role_id, permission_code=code))
    db.flush()


def provision_user_access(db, user, organisation_name=None, organisation_type=None):
    seed_rbac(db)
    legacy_role = user.data.get("role", "DRIVER")
    role_id = ROLE_IDS.get(legacy_role, ROLE_IDS["DRIVER"])
    platform_role = legacy_role in {"ADMIN", "system_super_user"}
    organisation = None
    if not platform_role:
        membership = db.scalar(
            select(OrganisationMembership)
            .where(OrganisationMembership.user_id == user.id)
            .order_by(OrganisationMembership.is_primary.desc())
            .limit(1)
        )
        if membership is None:
            organisation = Organisation(
                id=new_id("ORG"),
                name=organisation_name or user.data.get("companyName") or user.data.get("contactName"),
                type=organisation_type or legacy_role,
                verification=user.data.get("verification", "PENDING"),
                data={"createdByUserId": user.id},
                created_at=now(),
            )
            db.add(organisation)
            db.flush()
            membership = OrganisationMembership(
                id=new_id("OM"),
                organisation_id=organisation.id,
                user_id=user.id,
                status="ACTIVE",
                is_primary=True,
                created_at=now(),
            )
            db.add(membership)
        else:
            organisation = db.get(Organisation, membership.organisation_id)
        organisation_id = membership.organisation_id
    else:
        organisation_id = None

    assignment = db.scalar(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_id == role_id,
            UserRole.organisation_id.is_(None)
            if organisation_id is None
            else UserRole.organisation_id == organisation_id,
        )
    )
    if assignment is None:
        db.add(
            UserRole(
                id=new_id("UR"),
                user_id=user.id,
                role_id=role_id,
                organisation_id=organisation_id,
                assigned_by=None,
                created_at=now(),
            )
        )
    db.flush()
    return organisation


def primary_organisation_id(db, user):
    if user is None:
        return None
    membership = db.scalar(
        select(OrganisationMembership)
        .where(
            OrganisationMembership.user_id == user.id,
            OrganisationMembership.status == "ACTIVE",
        )
        .order_by(OrganisationMembership.is_primary.desc(), OrganisationMembership.created_at)
        .limit(1)
    )
    return membership.organisation_id if membership else None


def permission_codes(db, user, organisation_id=None):
    if user is None:
        return set()
    return set(
        db.scalars(
            select(RolePermission.permission_code)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(
                UserRole.user_id == user.id,
                (UserRole.organisation_id.is_(None))
                | (UserRole.organisation_id == organisation_id),
            )
        )
    )


def organisation_payloads(db, user):
    active = db.info.get("active_organisation_id")
    if user.data.get("role") in {"ADMIN", "system_super_user"}:
        return [
            {
                "id": organisation.id,
                "name": organisation.name,
                "type": organisation.type,
                "verification": organisation.verification,
                "isPrimary": organisation.id == active,
            }
            for organisation in db.scalars(select(Organisation).order_by(Organisation.name))
        ]
    memberships = list(
        db.scalars(
            select(OrganisationMembership).where(
                OrganisationMembership.user_id == user.id,
                OrganisationMembership.status == "ACTIVE",
            )
        )
    )
    result = []
    for membership in memberships:
        organisation = db.get(Organisation, membership.organisation_id)
        if organisation:
            result.append(
                {
                    "id": organisation.id,
                    "name": organisation.name,
                    "type": organisation.type,
                    "verification": organisation.verification,
                    "isPrimary": membership.is_primary,
                }
            )
    return result


def has_permission(db, user, code):
    effective = permission_codes(db, user, db.info.get("active_organisation_id"))
    return "platform.admin" in effective or code in effective


def require_permission(db, user, code):
    require(has_permission(db, user, code), "You do not have permission to perform this action", 403)


PROCEDURE_PERMISSIONS = {
    "organisations.list": "organisations.read",
    "organisations.members": "organisations.read",
    "organisations.create": "organisations.manage",
    "organisations.addMember": "organisations.manage",
    "organisations.update": "organisations.manage",
    "organisations.setMemberRoles": "organisations.manage",
    "organisations.removeMember": "organisations.manage",
    "organisations.switch": "organisations.read",
    "parties.list": "users.read",
    "parties.setVerification": "users.verify",
    "parties.findDriver": "dispatch.manage",
    "users.list": "users.read",
    "users.setVerification": "users.verify",
    "users.findDriver": "dispatch.manage",
    "loads.list": "loads.read",
    "loads.create": "loads.manage",
    "commands.prepareOffer": "matches.manage",
    "commands.acceptMatch": "matches.manage",
    "commands.confirmMatch": "matches.manage",
    "commands.acceptRequest": "matches.manage",
    "commands.advanceTrip": "trips.manage",
    "commands.cancelTrip": "trips.manage",
    "commands.reportIncident": "incidents.report",
    "operations.incidentAction": "incidents.manage",
    "finance.invoiceAction": "finance.manage",
    "messages.conversations": "messages.read",
    "messages.list": "messages.read",
    "messages.markRead": "messages.read",
    "messages.send": "messages.send",
    "notifications.list": "notifications.read",
    "notifications.unreadCount": "notifications.read",
    "notifications.markRead": "notifications.read",
    "matching.score": "maps.use",
    "matching.bodyTypesForCargo": "maps.use",
    "matching.roadDistanceKm": "maps.use",
    "pricing.quote": "maps.use",
    "pricing.quoteMoney": "maps.use",
    "routes.compute": "maps.use",
    "tracking.link": "tracking.read",
    "webhooks.list": "webhooks.manage",
    "webhooks.create": "webhooks.manage",
    "webhooks.setActive": "webhooks.manage",
    "webhooks.delete": "webhooks.manage",
    "webhooks.deliveries": "webhooks.manage",
}


def enforce_procedure_permission(db, user, procedure_name):
    permission = PROCEDURE_PERMISSIONS.get(procedure_name)
    if permission:
        require_permission(db, user, permission)
