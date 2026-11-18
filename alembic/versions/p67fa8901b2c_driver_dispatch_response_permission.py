"""ensure drivers can respond to assigned dispatches

Revision ID: p67fa8901b2c
Revises: o56ef68907e3
"""

from alembic import op
import sqlalchemy as sa


revision = "p67fa8901b2c"
down_revision = "o56ef68907e3"
branch_labels = None
depends_on = None


def upgrade():
    role_permissions = sa.table(
        "role_permissions",
        sa.column("role_id", sa.String),
        sa.column("permission_code", sa.String),
    )
    connection = op.get_bind()
    exists = connection.execute(
        sa.select(role_permissions.c.role_id).where(
            role_permissions.c.role_id == "ROLE-DRIVER",
            role_permissions.c.permission_code == "dispatch.respond",
        )
    ).first()
    if exists is None:
        op.bulk_insert(
            role_permissions,
            [{"role_id": "ROLE-DRIVER", "permission_code": "dispatch.respond"}],
        )


def downgrade():
    # This is a built-in Driver capability that existed before this repair
    # migration, so downgrading must not remove an administrator's prior data.
    pass
