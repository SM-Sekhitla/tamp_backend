"""trip messaging

Revision ID: g78d9ea12fc5
Revises: f67c8d901db4
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "g78d9ea12fc5"
down_revision = "f67c8d901db4"
branch_labels = None
depends_on = None


def upgrade():
    data = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    op.create_table("conversations", sa.Column("id", sa.String(80), primary_key=True), sa.Column("owner_id", sa.String(80), sa.ForeignKey("parties.id"), nullable=True), sa.Column("data", data, nullable=False), sa.Column("trip_id", sa.String(80), sa.ForeignKey("trips.id"), nullable=False))
    op.create_index("ix_conversations_trip_id", "conversations", ["trip_id"], unique=True)
    op.create_index("ix_conversations_owner_id", "conversations", ["owner_id"])
    op.create_table("messages", sa.Column("id", sa.String(80), primary_key=True), sa.Column("owner_id", sa.String(80), sa.ForeignKey("parties.id"), nullable=True), sa.Column("data", data, nullable=False), sa.Column("conversation_id", sa.String(80), sa.ForeignKey("conversations.id"), nullable=False))
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_owner_id", "messages", ["owner_id"])


def downgrade():
    op.drop_table("messages")
    op.drop_table("conversations")
