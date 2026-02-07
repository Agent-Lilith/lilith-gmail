"""drop sanitized_body

Revision ID: a5b6c7d8e9f0
Revises: f4a1b2c3d4e5
Create Date: 2026-02-07

Remove emails.sanitized_body; use body_redacted only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a5b6c7d8e9f0'
down_revision: Union[str, Sequence[str], None] = 'f4a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('emails', 'sanitized_body')


def downgrade() -> None:
    op.add_column(
        'emails',
        sa.Column('sanitized_body', sa.Text(), nullable=True),
    )
