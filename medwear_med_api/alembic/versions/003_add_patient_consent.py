"""add_patient_consent

Revision ID: 2cc97fc51290
Revises: f6d62f003a2f
Create Date: 2026-04-29 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '2cc97fc51290'
down_revision: Union[str, None] = 'f6d62f003a2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'patient_consent',
        sa.Column('id',              sa.Integer(),     nullable=False),
        sa.Column('patient_id',      sa.Integer(),     nullable=False),
        sa.Column('consent_type',    sa.String(100),   nullable=False),
        sa.Column('granted',         sa.Boolean(),     nullable=False, server_default='true'),
        sa.Column('granted_at',      sa.DateTime(),    nullable=False, server_default=sa.text('NOW()')),
        sa.Column('revoked_at',      sa.DateTime(),    nullable=True),
        sa.Column('consent_version', sa.String(20),    nullable=False, server_default='1.0'),
        sa.Column('granted_by',      sa.String(255),   nullable=False),
        sa.Column('project_id',      sa.String(100),   nullable=False),
        sa.Column('notes',           sa.Text(),        nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_patient_consent_patient_id', 'patient_consent', ['patient_id'])
    op.create_index('ix_patient_consent_project_id', 'patient_consent', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_patient_consent_project_id', table_name='patient_consent')
    op.drop_index('ix_patient_consent_patient_id', table_name='patient_consent')
    op.drop_table('patient_consent')
