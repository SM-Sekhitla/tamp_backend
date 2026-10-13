"""add versioned match offers and route quotes

Revision ID: b91d3a7c4e12
Revises: e2635e560400
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b91d3a7c4e12"
down_revision = "e2635e560400"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade():
    op.create_table(
        "route_quotes",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=80), nullable=True),
        sa.Column("data", json_type, nullable=False),
        sa.Column("load_id", sa.String(length=80), nullable=False),
        sa.Column("truck_id", sa.String(length=80), nullable=False),
        sa.Column("match_id", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["parties.id"]),
        sa.ForeignKeyConstraint(["load_id"], ["loads.id"]),
        sa.ForeignKeyConstraint(["truck_id"], ["trucks.id"]),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_route_quotes_owner_id"), "route_quotes", ["owner_id"])
    op.create_index(op.f("ix_route_quotes_load_id"), "route_quotes", ["load_id"])
    op.create_index(op.f("ix_route_quotes_truck_id"), "route_quotes", ["truck_id"])
    op.create_index(op.f("ix_route_quotes_match_id"), "route_quotes", ["match_id"])

    op.create_table(
        "match_offers",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=80), nullable=True),
        sa.Column("data", json_type, nullable=False),
        sa.Column("match_id", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["parties.id"]),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_match_offers_owner_id"), "match_offers", ["owner_id"])
    op.create_index(op.f("ix_match_offers_match_id"), "match_offers", ["match_id"])
    op.create_index(
        "one_offer_revision_per_match",
        "match_offers",
        ["match_id", sa.text("(data->>'revision')")],
        unique=True,
    )


def downgrade():
    op.drop_index("one_offer_revision_per_match", table_name="match_offers")
    op.drop_index(op.f("ix_match_offers_match_id"), table_name="match_offers")
    op.drop_index(op.f("ix_match_offers_owner_id"), table_name="match_offers")
    op.drop_table("match_offers")
    op.drop_index(op.f("ix_route_quotes_match_id"), table_name="route_quotes")
    op.drop_index(op.f("ix_route_quotes_truck_id"), table_name="route_quotes")
    op.drop_index(op.f("ix_route_quotes_load_id"), table_name="route_quotes")
    op.drop_index(op.f("ix_route_quotes_owner_id"), table_name="route_quotes")
    op.drop_table("route_quotes")
