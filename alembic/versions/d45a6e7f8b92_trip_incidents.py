"""trip incidents

Revision ID: d45a6e7f8b92
Revises: c34f5d6e7a81
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d45a6e7f8b92"
down_revision = "c34f5d6e7a81"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("owner_id", sa.String(length=80), sa.ForeignKey("parties.id"), nullable=True),
        sa.Column(
            "data",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("load_id", sa.String(length=80), sa.ForeignKey("loads.id"), nullable=False),
        sa.Column("trip_id", sa.String(length=80), sa.ForeignKey("trips.id"), nullable=False),
    )
    op.create_index("ix_incidents_owner_id", "incidents", ["owner_id"])
    op.create_index("ix_incidents_load_id", "incidents", ["load_id"])
    op.create_index("ix_incidents_trip_id", "incidents", ["trip_id"])


def downgrade():
    op.drop_table("incidents")
