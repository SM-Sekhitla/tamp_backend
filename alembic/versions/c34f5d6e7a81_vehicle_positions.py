"""add persisted vehicle positions

Revision ID: c34f5d6e7a81
Revises: b91d3a7c4e12
"""

import sqlalchemy as sa
from alembic import op

revision = "c34f5d6e7a81"
down_revision = "b91d3a7c4e12"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "vehicle_positions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trip_id", sa.String(length=80), nullable=False),
        sa.Column("truck_id", sa.String(length=80), nullable=False),
        sa.Column("reported_by", sa.String(length=80), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("accuracy_m", sa.Float(), nullable=True),
        sa.Column("speed_kph", sa.Float(), nullable=True),
        sa.Column("heading", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["reported_by"], ["parties.id"]),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["truck_id"], ["trucks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vehicle_positions_trip_recorded", "vehicle_positions", ["trip_id", "recorded_at"])
    op.create_index(op.f("ix_vehicle_positions_truck_id"), "vehicle_positions", ["truck_id"])
    op.create_index(op.f("ix_vehicle_positions_reported_by"), "vehicle_positions", ["reported_by"])
    op.create_index(op.f("ix_vehicle_positions_received_at"), "vehicle_positions", ["received_at"])


def downgrade():
    op.drop_index(op.f("ix_vehicle_positions_received_at"), table_name="vehicle_positions")
    op.drop_index(op.f("ix_vehicle_positions_reported_by"), table_name="vehicle_positions")
    op.drop_index(op.f("ix_vehicle_positions_truck_id"), table_name="vehicle_positions")
    op.drop_index("ix_vehicle_positions_trip_recorded", table_name="vehicle_positions")
    op.drop_table("vehicle_positions")
