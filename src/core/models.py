from datetime import datetime
from typing import List, Optional
from sqlalchemy import Integer, String, Text, Boolean, DateTime, BigInteger, ARRAY, ForeignKey, Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector

EMBEDDING_DIM = 768


class Base(DeclarativeBase):
    pass


class EmailAccount(Base):
    __tablename__ = "email_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_address: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String)
    oauth_token_encrypted: Mapped[bytes] = mapped_column(nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_history_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    emails: Mapped[List["Email"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    account_labels: Mapped[List["AccountLabel"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class AccountLabel(Base):
    __tablename__ = "account_labels"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    label_id: Mapped[str] = mapped_column(String, primary_key=True)
    label_name: Mapped[str] = mapped_column(String, nullable=False)

    account: Mapped["EmailAccount"] = relationship(back_populates="account_labels")


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("email_accounts.id", ondelete="CASCADE"))

    gmail_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String, nullable=False)
    history_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    subject: Mapped[Optional[str]] = mapped_column(Text)
    from_email: Mapped[str] = mapped_column(String, nullable=False)
    from_name: Mapped[Optional[str]] = mapped_column(String)
    to_emails: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), default=list)
    cc_emails: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String))
    bcc_emails: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String))
    reply_to: Mapped[Optional[str]] = mapped_column(String)

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    labels: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), default=list)
    is_read: Mapped[bool] = mapped_column(default=False)
    is_starred: Mapped[bool] = mapped_column(default=False)
    is_draft: Mapped[bool] = mapped_column(default=False)

    body_text: Mapped[Optional[str]] = mapped_column(Text)
    body_html: Mapped[Optional[str]] = mapped_column(Text)
    snippet: Mapped[Optional[str]] = mapped_column(Text)
    snippet_redacted: Mapped[Optional[str]] = mapped_column(Text)
    privacy_tier: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    body_redacted: Mapped[Optional[str]] = mapped_column(Text)
    transform_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    has_attachments: Mapped[bool] = mapped_column(default=False)
    attachment_count: Mapped[int] = mapped_column(default=0)

    subject_embedding: Mapped[Optional[Vector]] = mapped_column(Vector(EMBEDDING_DIM))
    body_embedding: Mapped[Optional[Vector]] = mapped_column(Vector(EMBEDDING_DIM))
    body_pooled_embedding: Mapped[Optional[Vector]] = mapped_column(Vector(EMBEDDING_DIM))

    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    account: Mapped["EmailAccount"] = relationship(back_populates="emails")
    attachments: Mapped[List["EmailAttachment"]] = relationship(back_populates="email", cascade="all, delete-orphan")
    chunks: Mapped[List["EmailChunk"]] = relationship(back_populates="email", cascade="all, delete-orphan")


class EmailChunk(Base):
    __tablename__ = "email_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), nullable=False)
    chunk_embedding: Mapped[Optional[Vector]] = mapped_column(Vector(EMBEDDING_DIM))
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_position: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_weight: Mapped[float] = mapped_column(Float, nullable=False)

    email: Mapped["Email"] = relationship(back_populates="chunks")


class EmailAttachment(Base):
    __tablename__ = "email_attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))

    gmail_attachment_id: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    extracted_text: Mapped[Optional[str]] = mapped_column(Text)
    extracted_embedding: Mapped[Optional[Vector]] = mapped_column(Vector(EMBEDDING_DIM))

    is_inline: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    email: Mapped["Email"] = relationship(back_populates="attachments")


class EmailThread(Base):
    __tablename__ = "email_threads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("email_accounts.id", ondelete="CASCADE"))
    gmail_thread_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    subject: Mapped[Optional[str]] = mapped_column(Text)
    participants: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), default=list)
    message_count: Mapped[int] = mapped_column(default=0)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    labels: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), default=list)

    summary: Mapped[Optional[str]] = mapped_column(Text)
    summary_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("email_accounts.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    emails_processed: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
