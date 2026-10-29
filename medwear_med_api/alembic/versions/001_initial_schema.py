"""initial_schema

Revision ID: 564725fdc673
Revises:
Create Date: 2026-04-21 16:54:36.330787

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '564725fdc673'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deleted_files")

    op.create_table(
        'measurement_batch',
        sa.Column('uuid', sa.VARCHAR(), primary_key=True),
        sa.Column('patient_id', sa.Integer(), nullable=False),
        sa.Column('device_id', sa.VARCHAR(), nullable=False),
        sa.Column('signal_type', sa.VARCHAR(), nullable=False),
        sa.Column('schema_id', sa.VARCHAR(), nullable=False),
        sa.Column('start_timestamp', sa.DateTime(), nullable=False),
        sa.Column('end_timestamp', sa.DateTime(), nullable=False),
        sa.Column('sampling_rate_hz', sa.Float(), nullable=False),
        sa.Column('sample_count', sa.Integer(), nullable=False),
        sa.Column('units', sa.VARCHAR(), nullable=False),
        sa.Column('metadata_json', postgresql.JSONB(), nullable=True),
        sa.Column('project_id', sa.VARCHAR(100), nullable=True),
        sa.Column('idempotency_key', sa.VARCHAR(), nullable=False, unique=False),
        sa.Column('checksum', sa.VARCHAR(64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('is_deleted', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_unique_constraint(
        'uq_idempotency_key', 'measurement_batch', ['idempotency_key']
    )

    op.create_table(
        'audit_log',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('username', sa.VARCHAR(), nullable=False),
        sa.Column('method', sa.VARCHAR(), nullable=False),
        sa.Column('path', sa.VARCHAR(), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('duration_ms', sa.Float(), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False,
                  server_default=sa.text('now()')),
    )

    op.create_table(
        'user_project',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('username', sa.VARCHAR(255), nullable=False),
        sa.Column('project_id', sa.VARCHAR(100), nullable=False),
        sa.Column('role', sa.VARCHAR(50), nullable=False,
                  server_default=sa.text("'collaborator'")),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('now()')),
    )


def downgrade() -> None:
    op.drop_table('user_project')
    op.drop_table('audit_log')
    op.drop_table('measurement_batch')
