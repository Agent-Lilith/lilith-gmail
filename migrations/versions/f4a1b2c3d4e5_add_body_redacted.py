"""add body_redacted

Revision ID: f4a1b2c3d4e5
Revises: e2a346f5b7c4
Create Date: 2026-02-07

Add emails.body_redacted: fully redacted body (PII + keys) for external display.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'e2a346f5b7c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'emails',
        sa.Column('body_redacted', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('emails', 'body_redacted')
