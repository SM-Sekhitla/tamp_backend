"""session lifecycle and organisation-scoped permission overrides

Revision ID: n45df57896d2
Revises: m23ef46785c1
"""
import sqlalchemy as sa
from alembic import op

revision = "n45df57896d2"
down_revision = "m23ef46785c1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_permission_overrides",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("user_id", sa.String(80), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organisation_id", sa.String(80), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("permission_code", sa.String(100), sa.ForeignKey("permissions.code", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.String(80), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "organisation_id", "permission_code"),
    )
    op.create_index("ix_user_permission_overrides_user_id", "user_permission_overrides", ["user_id"])
    op.create_index("ix_user_permission_overrides_organisation_id", "user_permission_overrides", ["organisation_id"])
    op.add_column("sessions", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sessions", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sessions", sa.Column("ip_address", sa.String(64), nullable=True))
    op.add_column("sessions", sa.Column("user_agent", sa.String(300), nullable=True))
    op.execute("UPDATE sessions SET created_at = CURRENT_TIMESTAMP, last_seen_at = CURRENT_TIMESTAMP")
    op.alter_column("sessions", "created_at", nullable=False)
    op.alter_column("sessions", "last_seen_at", nullable=False)
    op.create_index("ix_sessions_last_seen_at", "sessions", ["last_seen_at"])


def downgrade():
    op.drop_index("ix_sessions_last_seen_at", table_name="sessions")
    op.drop_column("sessions", "user_agent")
    op.drop_column("sessions", "ip_address")
    op.drop_column("sessions", "last_seen_at")
    op.drop_column("sessions", "created_at")
    op.drop_index("ix_user_permission_overrides_organisation_id", table_name="user_permission_overrides")
    op.drop_index("ix_user_permission_overrides_user_id", table_name="user_permission_overrides")
    op.drop_table("user_permission_overrides")
