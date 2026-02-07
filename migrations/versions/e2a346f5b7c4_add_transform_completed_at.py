"""add_transform_completed_at

Revision ID: e2a346f5b7c4
Revises: d1913354133b
Create Date: 2026-02-05

Add emails.transform_completed_at: set when transform succeeds so future runs skip
the email unless --force/--reset.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2a346f5b7c4'
down_revision: Union[str, Sequence[str], None] = 'd1913354133b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'emails',
        sa.Column('transform_completed_at', sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: emails that already have subject_embedding are considered completed
    op.execute(
        """
        UPDATE emails
        SET transform_completed_at = updated_at
        WHERE subject_embedding IS NOT NULL AND transform_completed_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column('emails', 'transform_completed_at')
