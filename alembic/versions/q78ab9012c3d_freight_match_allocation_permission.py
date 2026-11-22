"""repair freight allocation permissions for existing built-in roles

Revision ID: q78ab9012c3d
Revises: p67fa8901b2c
"""

from alembic import op
import sqlalchemy as sa


revision = "q78ab9012c3d"
down_revision = "p67fa8901b2c"
branch_labels = None
depends_on = None


def upgrade():
    """Give existing freight roles the allocation capability defined in code.

    ``seed_rbac`` intentionally does not restore permissions on an existing
    built-in role because an administrator may have removed them.  Deployments
    created before multi-truck allocation therefore never received this
    required capability.  This one-time migration aligns those existing roles
    without changing any user-specific deny override.
    """
    permissions = sa.table(
        "permissions",
        sa.column("code", sa.String),
        sa.column("description", sa.String),
    )
    role_permissions = sa.table(
        "role_permissions",
        sa.column("role_id", sa.String),
        sa.column("permission_code", sa.String),
    )
    connection = op.get_bind()

    permission_exists = connection.execute(
        sa.select(permissions.c.code).where(
            permissions.c.code == "matches.manage"
        )
    ).first()
    if permission_exists is None:
        op.bulk_insert(
            permissions,
            [{
                "code": "matches.manage",
                "description": "Review and accept matches, transporter offers and multi-truck allocations",
            }],
        )

    for role_id in ("ROLE-FREIGHT-OWNER", "ROLE-CARGO-OPERATOR"):
        exists = connection.execute(
            sa.select(role_permissions.c.role_id).where(
                role_permissions.c.role_id == role_id,
                role_permissions.c.permission_code == "matches.manage",
            )
        ).first()
        if exists is None:
            op.bulk_insert(
                role_permissions,
                [{"role_id": role_id, "permission_code": "matches.manage"}],
            )


def downgrade():
    # This repairs a built-in capability. Do not remove permissions that may
    # have already existed before this migration.
    pass
