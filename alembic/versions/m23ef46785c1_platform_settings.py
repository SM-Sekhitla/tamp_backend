"""platform matching settings

Revision ID: m23ef46785c1
Revises: l34ce35674b0
"""
import sqlalchemy as sa
from alembic import op

revision = "m23ef46785c1"
down_revision = "l34ce35674b0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(80), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade():
    op.drop_table("platform_settings")
