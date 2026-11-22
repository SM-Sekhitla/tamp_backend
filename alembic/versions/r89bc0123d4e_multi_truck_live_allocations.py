"""allow multiple live truck allocations for one load

Revision ID: r89bc0123d4e
Revises: q78ab9012c3d
"""

from alembic import op
import sqlalchemy as sa


revision = "r89bc0123d4e"
down_revision = "q78ab9012c3d"
branch_labels = None
depends_on = None


def upgrade():
    # The original indexes modelled every load as a single-truck engagement.
    # Fleet engagements need one active row per selected truck, while still
    # preventing the same truck from being allocated twice to the same load.
    op.drop_index("one_live_match_per_load", table_name="matches")
    op.create_index(
        "one_live_match_per_load_truck",
        "matches",
        ["load_id", "truck_id"],
        unique=True,
        postgresql_where=sa.text(
            "data->>'status' IN ('ACCEPTED', 'CONFIRMED')"
        ),
    )

    op.drop_index("one_reservation_per_load", table_name="reservations")
    op.create_index(
        "one_reservation_per_load_truck",
        "reservations",
        ["load_id", "truck_id"],
        unique=True,
        postgresql_where=sa.text("data->>'status' = 'RESERVED'"),
    )


def downgrade():
    op.drop_index("one_reservation_per_load_truck", table_name="reservations")
    op.create_index(
        "one_reservation_per_load",
        "reservations",
        ["load_id"],
        unique=True,
        postgresql_where=sa.text("data->>'status' = 'RESERVED'"),
    )

    op.drop_index("one_live_match_per_load_truck", table_name="matches")
    op.create_index(
        "one_live_match_per_load",
        "matches",
        ["load_id"],
        unique=True,
        postgresql_where=sa.text(
            "data->>'status' IN ('ACCEPTED', 'CONFIRMED')"
        ),
    )
