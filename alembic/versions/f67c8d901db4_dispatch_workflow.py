"""dispatch workflow

Revision ID: f67c8d901db4
Revises: e56b7f809ca3
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f67c8d901db4"
down_revision = "e56b7f809ca3"
branch_labels = None
depends_on = None


def upgrade():
    data = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    op.create_table("dispatches", sa.Column("id", sa.String(80), primary_key=True), sa.Column("owner_id", sa.String(80), sa.ForeignKey("parties.id"), nullable=True), sa.Column("data", data, nullable=False), sa.Column("trip_id", sa.String(80), sa.ForeignKey("trips.id"), nullable=False), sa.Column("truck_id", sa.String(80), sa.ForeignKey("trucks.id"), nullable=False))
    op.create_index("ix_dispatches_owner_id", "dispatches", ["owner_id"])
    op.create_index("ix_dispatches_trip_id", "dispatches", ["trip_id"])
    op.create_index("ix_dispatches_truck_id", "dispatches", ["truck_id"])


def downgrade():
    op.drop_table("dispatches")
