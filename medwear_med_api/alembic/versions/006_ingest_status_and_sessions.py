"""ingest_status_and_sessions

Revision ID: 9a1f3c2e7b44
Revises: 83241c70658c
Create Date: 2026-10-23 00:00:00.000000

Adds measurement_batch.ingest_status (pending until both stores are written)
and measurement_batch.session_id, and the ingest_session table that holds
stream metadata registered once per session.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '9a1f3c2e7b44'
down_revision: Union[str, Sequence[str], None] = '83241c70658c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('measurement_batch', sa.Column(
        'ingest_status', sa.VARCHAR(16), nullable=False, server_default='complete'))
    op.add_column('measurement_batch', sa.Column('session_id', sa.VARCHAR(64), nullable=True))
    op.create_index('ix_measurement_batch_pending', 'measurement_batch', ['ingest_status'],
                    postgresql_where=sa.text("ingest_status = 'pending'"))
    op.create_table(
        'ingest_session',
        sa.Column('session_id', sa.VARCHAR(64), primary_key=True),
        sa.Column('patient_id', sa.VARCHAR(255), nullable=False),
        sa.Column('device_id', sa.VARCHAR(255), nullable=False),
        sa.Column('signal_type', sa.VARCHAR(50), nullable=False),
        sa.Column('schema_id', sa.VARCHAR(100), nullable=False),
        sa.Column('sampling_rate_hz', sa.Float(), nullable=False),
        sa.Column('units', sa.VARCHAR(20), nullable=False),
        sa.Column('header_json', postgresql.JSONB(), nullable=True),
        sa.Column('project_id', sa.VARCHAR(100), nullable=False),
        sa.Column('created_by', sa.VARCHAR(255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('NOW()')),
    )


def downgrade() -> None:
    op.drop_table('ingest_session')
    op.drop_index('ix_measurement_batch_pending', table_name='measurement_batch')
    op.drop_column('measurement_batch', 'session_id')
    op.drop_column('measurement_batch', 'ingest_status')
