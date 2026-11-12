"""driver compliance documents

Revision ID: l34ce35674b0
Revises: k12bd24563a9
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "l34ce35674b0"
down_revision = "k12bd24563a9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "driverDocuments",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("owner_id", sa.String(80), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("organisation_id", sa.String(80), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("driver_id", sa.String(80), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index("ix_driverDocuments_owner_id", "driverDocuments", ["owner_id"])
    op.create_index("ix_driverDocuments_organisation_id", "driverDocuments", ["organisation_id"])
    op.create_index("ix_driverDocuments_driver_id", "driverDocuments", ["driver_id"])
    op.create_index("ix_driverDocuments_expiry", "driverDocuments", [sa.text("(data->>'expiresAt')")])


def downgrade():
    op.drop_table("driverDocuments")
