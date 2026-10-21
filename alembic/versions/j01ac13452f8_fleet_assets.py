"""separate permanent vehicles from availability postings

Revision ID: j01ac13452f8
Revises: i90fb02341e7
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "j01ac13452f8"
down_revision = "i90fb02341e7"
branch_labels = None
depends_on = None


def _asset_table(name):
    op.create_table(
        name,
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("owner_id", sa.String(80), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("organisation_id", sa.String(80), sa.ForeignKey("organisations.id"), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index(f"ix_{name}_owner_id", name, ["owner_id"])
    op.create_index(f"ix_{name}_organisation_id", name, ["organisation_id"])


def upgrade():
    _asset_table("vehicles")
    op.create_index("uq_vehicles_org_registration", "vehicles", ["organisation_id", sa.text("(upper(data->>'registration'))")], unique=True)
    _asset_table("vehicleDriverAssignments")
    op.add_column("vehicleDriverAssignments", sa.Column("vehicle_id", sa.String(80), nullable=False))
    op.add_column("vehicleDriverAssignments", sa.Column("driver_id", sa.String(80), nullable=False))
    op.create_foreign_key("fk_vehicle_assignments_vehicle", "vehicleDriverAssignments", "vehicles", ["vehicle_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_vehicle_assignments_driver", "vehicleDriverAssignments", "users", ["driver_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_vehicle_assignments_vehicle_id", "vehicleDriverAssignments", ["vehicle_id"])
    op.create_index("ix_vehicle_assignments_driver_id", "vehicleDriverAssignments", ["driver_id"])

    op.add_column("trucks", sa.Column("vehicle_id", sa.String(80), nullable=True))
    op.execute(sa.text("""INSERT INTO vehicles (id, owner_id, organisation_id, data)
        SELECT 'VH-' || md5(id), owner_id, organisation_id,
          jsonb_build_object(
            'id', 'VH-' || md5(id), 'ownerId', data->>'transporterId',
            'registration', data->>'registration', 'bodyType', data->>'bodyType',
            'payloadCapacityKg', data->'payloadCapacityKg', 'photos', COALESCE(data->'photos', '[]'::jsonb),
            'documents', COALESCE(data->'documents', '[]'::jsonb),
            'verification', 'PENDING', 'status', 'ACTIVE',
            'createdAt', data->>'createdAt', 'updatedAt', data->>'createdAt'
          ) || CASE WHEN data ? 'volumeCapacityM3' THEN jsonb_build_object('volumeCapacityM3', data->'volumeCapacityM3') ELSE '{}'::jsonb END
            || CASE WHEN data ? 'licenceExpiry' THEN jsonb_build_object('licenceExpiry', data->>'licenceExpiry') ELSE '{}'::jsonb END
        FROM trucks"""))
    op.execute(sa.text("""UPDATE trucks SET vehicle_id = 'VH-' || md5(id),
        data = data || jsonb_build_object('vehicleId', 'VH-' || md5(id))"""))
    op.alter_column("trucks", "vehicle_id", nullable=False)
    op.create_foreign_key("fk_trucks_vehicle", "trucks", "vehicles", ["vehicle_id"], ["id"])
    op.create_index("ix_trucks_vehicle_id", "trucks", ["vehicle_id"])


def downgrade():
    op.drop_index("ix_trucks_vehicle_id", table_name="trucks")
    op.drop_constraint("fk_trucks_vehicle", "trucks", type_="foreignkey")
    op.drop_column("trucks", "vehicle_id")
    op.drop_table("vehicleDriverAssignments")
    op.drop_index("uq_vehicles_org_registration", table_name="vehicles")
    op.drop_table("vehicles")
