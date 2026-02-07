"""upgrade_to_multi_embed_768

Revision ID: d1913354133b
Revises: ba0926d0ae32
Create Date: 2026-02-05 15:24:55.004451

Upgrades an existing DB (old schema with emails.embedding 384d) to the multi-level
embedding schema: subject_embedding, body_embedding, body_pooled_embedding (768d),
and email_chunks table. Preserves all existing email rows; no transformations required
before running this.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = 'd1913354133b'
down_revision: Union[str, Sequence[str], None] = 'ba0926d0ae32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    email_cols = [c["name"] for c in insp.get_columns("emails")]
    has_old_embedding = "embedding" in email_cols
    has_new_embeddings = "subject_embedding" in email_cols
    has_chunks = "email_chunks" in insp.get_table_names()

    if has_old_embedding and not has_new_embeddings:
        # Old schema (single embedding 384d): add new columns, drop old, create chunks
        op.add_column("emails", sa.Column("subject_embedding", Vector(768), nullable=True))
        op.add_column("emails", sa.Column("body_embedding", Vector(768), nullable=True))
        op.add_column("emails", sa.Column("body_pooled_embedding", Vector(768), nullable=True))
        op.drop_column("emails", "embedding")
    if not has_chunks:
        op.create_table(
            "email_chunks",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("email_id", sa.BigInteger(), nullable=False),
            sa.Column("chunk_embedding", Vector(768), nullable=True),
            sa.Column("chunk_text", sa.Text(), nullable=False),
            sa.Column("chunk_position", sa.Integer(), nullable=False),
            sa.Column("chunk_weight", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["email_id"], ["emails.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    # Attachment embedding 384 -> 768 only if we just migrated emails (old DB); initial_schema already has 768
    if has_old_embedding:
        op.drop_column("email_attachments", "extracted_embedding")
        op.add_column("email_attachments", sa.Column("extracted_embedding", Vector(768), nullable=True))


def downgrade() -> None:
    op.drop_column('email_attachments', 'extracted_embedding')
    op.add_column('email_attachments', sa.Column('extracted_embedding', Vector(384), nullable=True))
    op.drop_table('email_chunks')
    op.add_column('emails', sa.Column('embedding', Vector(384), nullable=True))
    op.drop_column('emails', 'body_pooled_embedding')
    op.drop_column('emails', 'body_embedding')
    op.drop_column('emails', 'subject_embedding')
