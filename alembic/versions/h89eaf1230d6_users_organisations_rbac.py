"""users, organisations and role-based access control

Revision ID: h89eaf1230d6
Revises: g78d9ea12fc5
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "h89eaf1230d6"
down_revision = "g78d9ea12fc5"
branch_labels = None
depends_on = None

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

ROLE_PERMISSIONS = {
    "system_super_user": set(PERMISSIONS),
    "ADMIN": set(PERMISSIONS),
    "FREIGHT_OWNER": {"organisations.read", "organisations.manage", "loads.read", "loads.manage", "trucks.read", "matches.read", "matches.manage", "trips.read", "trips.manage", "incidents.report", "finance.read", "messages.read", "messages.send", "notifications.read", "tracking.read", "maps.use"},
    "TRANSPORTER": {"organisations.read", "organisations.manage", "loads.read", "trucks.read", "trucks.manage", "matches.read", "matches.manage", "trips.read", "trips.manage", "dispatch.manage", "incidents.report", "finance.read", "messages.read", "messages.send", "notifications.read", "tracking.read", "tracking.publish", "maps.use"},
    "DRIVER": {"organisations.read", "trucks.read", "trips.read", "trips.manage", "dispatch.respond", "incidents.report", "messages.read", "messages.send", "notifications.read", "tracking.read", "tracking.publish", "maps.use"},
}


def upgrade():
    op.rename_table("parties", "users")
    op.execute("ALTER INDEX IF EXISTS ix_parties_email RENAME TO ix_users_email")
    op.alter_column("sessions", "party_id", new_column_name="user_id")
    op.execute("ALTER INDEX IF EXISTS ix_sessions_party_id RENAME TO ix_sessions_user_id")

    data = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    op.create_table(
        "organisations",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("verification", sa.String(20), nullable=False),
        sa.Column("data", data, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_organisations_name", "organisations", ["name"])
    op.create_index("ix_organisations_type", "organisations", ["type"])
    op.create_index("ix_organisations_verification", "organisations", ["verification"])

    op.create_table(
        "roles",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("system", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_roles_name", "roles", ["name"], unique=True)
    op.create_index("ix_roles_scope", "roles", ["scope"])
    op.create_table(
        "permissions",
        sa.Column("code", sa.String(100), primary_key=True),
        sa.Column("description", sa.String(300), nullable=False),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.String(80), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_code", sa.String(100), sa.ForeignKey("permissions.code", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "organisation_memberships",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("organisation_id", sa.String(80), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(80), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organisation_id", "user_id"),
    )
    for column in ["organisation_id", "user_id", "status"]:
        op.create_index(f"ix_organisation_memberships_{column}", "organisation_memberships", [column])
    op.create_table(
        "user_roles",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("user_id", sa.String(80), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", sa.String(80), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organisation_id", sa.String(80), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("assigned_by", sa.String(80), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "role_id", "organisation_id"),
    )
    for column in ["user_id", "role_id", "organisation_id"]:
        op.create_index(f"ix_user_roles_{column}", "user_roles", [column])

    permissions_table = sa.table("permissions", sa.column("code", sa.String), sa.column("description", sa.String))
    op.bulk_insert(permissions_table, [{"code": code, "description": description} for code, description in PERMISSIONS.items()])
    roles_table = sa.table("roles", sa.column("id", sa.String), sa.column("name", sa.String), sa.column("scope", sa.String), sa.column("description", sa.String), sa.column("system", sa.Boolean))
    role_names = {"system_super_user": "System Super User", "ADMIN": "Platform Administrator", "FREIGHT_OWNER": "Freight Owner", "TRANSPORTER": "Transporter", "DRIVER": "Driver"}
    op.bulk_insert(roles_table, [{"id": ident, "name": role_names[legacy], "scope": "PLATFORM" if legacy in {"system_super_user", "ADMIN"} else "ORGANISATION", "description": f"Built-in {role_names[legacy]} role", "system": True} for legacy, ident in ROLE_IDS.items()])
    role_permissions_table = sa.table("role_permissions", sa.column("role_id", sa.String), sa.column("permission_code", sa.String))
    op.bulk_insert(role_permissions_table, [{"role_id": ROLE_IDS[legacy], "permission_code": code} for legacy, codes in ROLE_PERMISSIONS.items() for code in sorted(codes)])

    now = datetime.now(timezone.utc).isoformat()
    op.execute(sa.text("""
        INSERT INTO organisations (id, name, type, verification, data, created_at)
        SELECT 'ORG-' || substr(md5(id), 1, 24),
               COALESCE(NULLIF(data->>'companyName', ''), NULLIF(data->>'contactName', ''), email),
               COALESCE(data->>'role', 'DRIVER'),
               COALESCE(data->>'verification', 'PENDING'),
               jsonb_build_object('createdByUserId', id, 'migrated', true),
               COALESCE(NULLIF(data->>'createdAt', '')::timestamptz, CAST(:now AS timestamptz))
        FROM users
        WHERE COALESCE(data->>'role', '') NOT IN ('ADMIN', 'system_super_user')
    """).bindparams(now=now))
    op.execute(sa.text("""
        INSERT INTO organisation_memberships (id, organisation_id, user_id, status, is_primary, created_at)
        SELECT 'OM-' || substr(md5(id), 1, 24), 'ORG-' || substr(md5(id), 1, 24), id, 'ACTIVE', true,
               COALESCE(NULLIF(data->>'createdAt', '')::timestamptz, CAST(:now AS timestamptz))
        FROM users
        WHERE COALESCE(data->>'role', '') NOT IN ('ADMIN', 'system_super_user')
    """).bindparams(now=now))
    op.execute(sa.text("""
        INSERT INTO user_roles (id, user_id, role_id, organisation_id, assigned_by, created_at)
        SELECT 'UR-' || substr(md5(id), 1, 24), id,
               CASE data->>'role'
                 WHEN 'system_super_user' THEN 'ROLE-SYSTEM-SUPER-USER'
                 WHEN 'ADMIN' THEN 'ROLE-PLATFORM-ADMIN'
                 WHEN 'FREIGHT_OWNER' THEN 'ROLE-FREIGHT-OWNER'
                 WHEN 'TRANSPORTER' THEN 'ROLE-TRANSPORTER'
                 ELSE 'ROLE-DRIVER'
               END,
               CASE WHEN data->>'role' IN ('ADMIN', 'system_super_user') THEN NULL
                    ELSE 'ORG-' || substr(md5(id), 1, 24) END,
               NULL, COALESCE(NULLIF(data->>'createdAt', '')::timestamptz, CAST(:now AS timestamptz))
        FROM users
    """).bindparams(now=now))


def downgrade():
    op.drop_table("user_roles")
    op.drop_table("organisation_memberships")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("organisations")
    op.alter_column("sessions", "user_id", new_column_name="party_id")
    op.execute("ALTER INDEX IF EXISTS ix_sessions_user_id RENAME TO ix_sessions_party_id")
    op.rename_table("users", "parties")
    op.execute("ALTER INDEX IF EXISTS ix_users_email RENAME TO ix_parties_email")
