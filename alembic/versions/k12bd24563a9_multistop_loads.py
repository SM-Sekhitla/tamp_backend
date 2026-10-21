"""backfill ordered pickup and delivery stops

Revision ID: k12bd24563a9
Revises: j01ac13452f8
"""

import sqlalchemy as sa

from alembic import op

revision = "k12bd24563a9"
down_revision = "j01ac13452f8"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""UPDATE loads
        SET data = data || jsonb_build_object('stops', jsonb_build_array(
          jsonb_build_object(
            'id', 'LS-' || md5(id || '-pickup'), 'sequence', 0, 'kind', 'PICKUP',
            'place', data->'origin', 'window', data->'pickupWindow',
            'cargoWeightKg', data->'weightKg', 'status', 'PENDING'
          ),
          jsonb_build_object(
            'id', 'LS-' || md5(id || '-dropoff'), 'sequence', 1, 'kind', 'DROPOFF',
            'place', data->'destination',
            'window', jsonb_build_object('from', data->>'deliveryBy', 'to', data->>'deliveryBy'),
            'cargoWeightKg', data->'weightKg', 'status', 'PENDING'
          )
        )) WHERE NOT (data ? 'stops') OR jsonb_array_length(data->'stops') = 0"""))


def downgrade():
    op.execute(sa.text("UPDATE loads SET data = data - 'stops'"))
