"""add snippet_redacted

Revision ID: g5b6c7d8e9f1
Revises: a5b6c7d8e9f0
Create Date: 2026-02-07

Add emails.snippet_redacted: redacted snippet for display; SENSITIVE/PERSONAL use fixed string.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'g5b6c7d8e9f1'
down_revision: Union[str, Sequence[str], None] = 'a5b6c7d8e9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'emails',
        sa.Column('snippet_redacted', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('emails', 'snippet_redacted')
