"""freight owner customers, locations and templates

Revision ID: o56ef68907e3
Revises: n45df57896d2
"""
import sqlalchemy as sa
from alembic import op

revision = "o56ef68907e3"
down_revision = "n45df57896d2"
branch_labels = None
depends_on = None


def _record_table(name):
    op.create_table(
        name,
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("owner_id", sa.String(80), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("organisation_id", sa.String(80), sa.ForeignKey("organisations.id"), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index(f"ix_{name}_owner_id", name, ["owner_id"])
    op.create_index(f"ix_{name}_organisation_id", name, ["organisation_id"])


def upgrade():
    for name in ("freightCustomers", "savedLocations", "loadTemplates"):
        _record_table(name)


def downgrade():
    for name in reversed(("freightCustomers", "savedLocations", "loadTemplates")):
        op.drop_index(f"ix_{name}_organisation_id", table_name=name)
        op.drop_index(f"ix_{name}_owner_id", table_name=name)
        op.drop_table(name)
