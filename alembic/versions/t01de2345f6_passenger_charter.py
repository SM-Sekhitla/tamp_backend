"""Store passenger charter requests and agreed bookings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "t01de2345f6"
down_revision = "s90cd1234e5f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "charterBookings",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("owner_id", sa.String(80), sa.ForeignKey("users.id")),
        sa.Column("organisation_id", sa.String(80), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("supplier_id", sa.String(80), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("data", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    )
    for column in ("owner_id", "organisation_id", "supplier_id"):
        op.create_index(f"ix_charterBookings_{column}", "charterBookings", [column])


def downgrade():
    op.drop_table("charterBookings")
