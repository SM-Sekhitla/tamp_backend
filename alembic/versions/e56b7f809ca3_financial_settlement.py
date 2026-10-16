"""financial settlement

Revision ID: e56b7f809ca3
Revises: d45a6e7f8b92
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e56b7f809ca3"
down_revision = "d45a6e7f8b92"
branch_labels = None
depends_on = None


def _data():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade():
    op.create_table("invoices", sa.Column("id", sa.String(80), primary_key=True), sa.Column("owner_id", sa.String(80), sa.ForeignKey("parties.id"), nullable=True), sa.Column("data", _data(), nullable=False), sa.Column("load_id", sa.String(80), sa.ForeignKey("loads.id"), nullable=False), sa.Column("trip_id", sa.String(80), sa.ForeignKey("trips.id"), nullable=False), sa.Column("match_id", sa.String(80), sa.ForeignKey("matches.id"), nullable=False))
    for column in ["owner_id", "load_id", "trip_id", "match_id"]:
        op.create_index(f"ix_invoices_{column}", "invoices", [column])
    op.create_index("uq_invoices_trip_id", "invoices", ["trip_id"], unique=True)
    op.create_table("transactions", sa.Column("id", sa.String(80), primary_key=True), sa.Column("owner_id", sa.String(80), sa.ForeignKey("parties.id"), nullable=True), sa.Column("data", _data(), nullable=False), sa.Column("invoice_id", sa.String(80), sa.ForeignKey("invoices.id"), nullable=False))
    op.create_index("ix_transactions_owner_id", "transactions", ["owner_id"])
    op.create_index("ix_transactions_invoice_id", "transactions", ["invoice_id"])


def downgrade():
    op.drop_table("transactions")
    op.drop_table("invoices")
