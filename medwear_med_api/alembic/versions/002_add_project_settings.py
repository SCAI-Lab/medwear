"""add_project_settings

Revision ID: f6d62f003a2f
Revises: 564725fdc673
Create Date: 2026-04-28 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f6d62f003a2f'
down_revision: Union[str, Sequence[str], None] = '564725fdc673'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'project_settings',
        sa.Column('project_id', sa.String(length=100), primary_key=True),
        sa.Column('retention_days', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.String(length=255), nullable=False, server_default='system'),
    )


def downgrade() -> None:
    op.drop_table('project_settings')
