"""patient_id_to_varchar

Revision ID: 83241c70658c
Revises: 3ddb0d11ffa1
Create Date: 2026-05-11 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '83241c70658c'
down_revision: Union[str, Sequence[str], None] = '3ddb0d11ffa1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'measurement_batch', 'patient_id',
        existing_type=sa.BigInteger(),
        type_=sa.VARCHAR(255),
        existing_nullable=False,
        postgresql_using='patient_id::VARCHAR',
    )
    op.alter_column(
        'patient_consent', 'patient_id',
        existing_type=sa.Integer(),
        type_=sa.VARCHAR(255),
        existing_nullable=False,
        postgresql_using='patient_id::VARCHAR',
    )


def downgrade() -> None:
    op.alter_column(
        'measurement_batch', 'patient_id',
        existing_type=sa.VARCHAR(255),
        type_=sa.BigInteger(),
        existing_nullable=False,
        postgresql_using='patient_id::BIGINT',
    )
    op.alter_column(
        'patient_consent', 'patient_id',
        existing_type=sa.VARCHAR(255),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using='patient_id::INTEGER',
    )
