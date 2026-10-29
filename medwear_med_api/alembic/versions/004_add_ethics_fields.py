"""add_ethics_fields_to_patient_consent

Revision ID: 3ddb0d11ffa1
Revises: 2cc97fc51290
Create Date: 2026-04-29 00:01:00.000000

Adds ETH Ethics Commission required fields to patient_consent table.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '3ddb0d11ffa1'
down_revision: Union[str, None] = '2cc97fc51290'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('patient_consent',
        sa.Column('ethics_protocol_number', sa.String(100), nullable=True))
    op.add_column('patient_consent',
        sa.Column('consent_purpose', sa.Text(), nullable=True))
    op.add_column('patient_consent',
        sa.Column('legal_basis', sa.String(100), nullable=False,
                  server_default='Art. 9(2)(a) GDPR'))
    op.add_column('patient_consent',
        sa.Column('data_retention_days', sa.Integer(), nullable=True))
    op.add_column('patient_consent',
        sa.Column('consent_expiry_date', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('patient_consent', 'consent_expiry_date')
    op.drop_column('patient_consent', 'data_retention_days')
    op.drop_column('patient_consent', 'legal_basis')
    op.drop_column('patient_consent', 'consent_purpose')
    op.drop_column('patient_consent', 'ethics_protocol_number')
