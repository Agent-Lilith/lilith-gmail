"""add fulltext search tsvector columns

Revision ID: i7d8e9f3b4c5
Revises: h6c7d8e9f2a3
Create Date: 2026-02-11

Add search_tsv tsvector column to emails table with GIN index.
Backfill from existing subject + body_text using a trigger for future inserts.
This does NOT require re-running the embedding transform pipeline.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "i7d8e9f3b4c5"
down_revision: Union[str, Sequence[str], None] = "h6c7d8e9f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add tsvector column
    op.execute("ALTER TABLE emails ADD COLUMN search_tsv tsvector")

    # Backfill tsvector from existing data (subject + body_text)
    # This runs purely on existing text columns -- no embedding needed
    op.execute("""
        UPDATE emails
        SET search_tsv = to_tsvector('simple',
            COALESCE(subject, '') || ' ' ||
            COALESCE(from_email, '') || ' ' ||
            COALESCE(from_name, '') || ' ' ||
            COALESCE(body_text, '')
        )
        WHERE body_text IS NOT NULL
    """)

    # GIN index for fast fulltext lookup (must run outside transaction)
    op.create_index(
        "ix_emails_search_tsv",
        "emails",
        ["search_tsv"],
        postgresql_using="gin",
    )

    # Trigger to auto-update tsvector on INSERT or UPDATE
    op.execute("""
        CREATE OR REPLACE FUNCTION emails_search_tsv_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_tsv := to_tsvector('simple',
                COALESCE(NEW.subject, '') || ' ' ||
                COALESCE(NEW.from_email, '') || ' ' ||
                COALESCE(NEW.from_name, '') || ' ' ||
                COALESCE(NEW.body_text, '')
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE TRIGGER trg_emails_search_tsv
        BEFORE INSERT OR UPDATE OF subject, from_email, from_name, body_text
        ON emails
        FOR EACH ROW
        EXECUTE FUNCTION emails_search_tsv_update();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_emails_search_tsv ON emails")
    op.execute("DROP FUNCTION IF EXISTS emails_search_tsv_update()")
    op.execute("DROP INDEX IF EXISTS ix_emails_search_tsv")
    op.execute("ALTER TABLE emails DROP COLUMN IF EXISTS search_tsv")
