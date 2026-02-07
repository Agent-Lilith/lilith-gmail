"""add_account_labels

Revision ID: h6c7d8e9f2a3
Revises: g5b6c7d8e9f1
Create Date: 2026-02-07

Add account_labels table: Gmail label id -> name per account for readable labels in MCP.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h6c7d8e9f2a3"
down_revision: Union[str, Sequence[str], None] = "g5b6c7d8e9f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "account_labels",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("label_id", sa.String(), nullable=False),
        sa.Column("label_name", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["email_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id", "label_id"),
    )


def downgrade() -> None:
    op.drop_table("account_labels")
