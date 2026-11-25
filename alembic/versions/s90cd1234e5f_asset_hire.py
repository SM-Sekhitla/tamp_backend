"""Add Asset Hire listings, reservations and freight links."""
from alembic import op
import sqlalchemy as sa

revision = "s90cd1234e5f"
down_revision = "r89bc0123d4e"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("hireAssets", "assetBookings", "assetMoves"):
        columns = [
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("owner_id", sa.String(80), sa.ForeignKey("users.id")),
            sa.Column("organisation_id", sa.String(80), sa.ForeignKey("organisations.id")),
            sa.Column("data", sa.JSON(), nullable=False),
        ]
        indexed = ["owner_id", "organisation_id"]
        if table == "assetBookings":
            columns.extend([
                sa.Column("asset_id", sa.String(80), sa.ForeignKey("hireAssets.id"), nullable=False),
                sa.Column("supplier_id", sa.String(80), sa.ForeignKey("organisations.id"), nullable=False),
            ])
            indexed.extend(["asset_id", "supplier_id"])
        op.create_table(table, *columns)
        for column in indexed:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in ("assetMoves", "assetBookings", "hireAssets"):
        op.drop_table(table)
