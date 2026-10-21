"""active organisation sessions and organisation-owned domain records

Revision ID: i90fb02341e7
Revises: h89eaf1230d6
"""

import sqlalchemy as sa

from alembic import op

revision = "i90fb02341e7"
down_revision = "h89eaf1230d6"
branch_labels = None
depends_on = None

TABLES = [
    "loads", "trucks", "matches", "route_quotes", "match_offers", "trips",
    "ratings", "disputes", "acceptances", "reservations", "audit", "fuelLogs",
    "maintenance", "proofs", "incidents", "invoices", "transactions", "dispatches",
    "conversations", "messages", "notifications", "uploads", "webhooks", "deliveries",
]


def upgrade():
    op.add_column("sessions", sa.Column("active_organisation_id", sa.String(80), nullable=True))
    op.create_foreign_key("fk_sessions_active_organisation", "sessions", "organisations", ["active_organisation_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_sessions_active_organisation_id", "sessions", ["active_organisation_id"])
    for table in TABLES:
        op.add_column(table, sa.Column("organisation_id", sa.String(80), nullable=True))
        op.create_foreign_key(f"fk_{table}_organisation", table, "organisations", ["organisation_id"], ["id"])
        op.create_index(f"ix_{table}_organisation_id", table, ["organisation_id"])
        op.execute(sa.text(f'''UPDATE "{table}" AS record
            SET organisation_id = membership.organisation_id
            FROM organisation_memberships AS membership
            WHERE membership.user_id = record.owner_id
              AND membership.status = 'ACTIVE'
              AND membership.is_primary = true
              AND record.organisation_id IS NULL'''))
    op.execute(sa.text("""UPDATE sessions AS session
        SET active_organisation_id = membership.organisation_id
        FROM organisation_memberships AS membership
        WHERE membership.user_id = session.user_id
          AND membership.status = 'ACTIVE'
          AND membership.is_primary = true"""))


def downgrade():
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_organisation_id", table_name=table)
        op.drop_constraint(f"fk_{table}_organisation", table, type_="foreignkey")
        op.drop_column(table, "organisation_id")
    op.drop_index("ix_sessions_active_organisation_id", table_name="sessions")
    op.drop_constraint("fk_sessions_active_organisation", "sessions", type_="foreignkey")
    op.drop_column("sessions", "active_organisation_id")
