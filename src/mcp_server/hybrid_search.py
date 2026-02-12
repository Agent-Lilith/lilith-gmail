"""Hybrid search engine for emails: structured + fulltext + vector.

Each method runs independently and returns scored results.
The engine merges, deduplicates, and produces SearchResultV1-compatible output.
"""

import logging
import time
from datetime import date, datetime, time as dtime
from typing import Any

from sqlalchemy import func, select, text, literal_column
from sqlalchemy.orm import Session

from core.embeddings import Embedder
from core.models import AccountLabel, Email

from .external_format import external_email_to_dict, to_external_email

logger = logging.getLogger(__name__)

PRIVACY_MODE_EXTERNAL = "external"


def _parse_date_bound(s: str, end_of_day: bool = False) -> datetime:
    s = s.strip()
    if "T" in s or " " in s:
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    d = date.fromisoformat(s)
    if end_of_day:
        return datetime.combine(d, dtime(23, 59, 59, 999999))
    return datetime.combine(d, dtime(0, 0, 0))


def _transformed_only(stmt):
    return stmt.where(Email.transform_completed_at.isnot(None))


def _load_label_maps(db: Session, account_ids: list[int]) -> dict[int, dict[str, str]]:
    if not account_ids:
        return {}
    rows = db.execute(
        select(AccountLabel).where(AccountLabel.account_id.in_(account_ids))
    ).scalars().all()
    out: dict[int, dict[str, str]] = {aid: {} for aid in account_ids}
    for al in rows:
        out[al.account_id][al.label_id] = al.label_name
    return out


def _apply_filters(stmt, filters: list[dict[str, Any]] | None, account_id: int | None):
    """Apply filter clauses to a query statement."""
    stmt = stmt.where(Email.deleted_at.is_(None))
    stmt = _transformed_only(stmt)

    if account_id is not None:
        stmt = stmt.where(Email.account_id == account_id)

    if not filters:
        return stmt

    for f in filters:
        field = f.get("field", "")
        op = f.get("operator", "eq")
        value = f.get("value")

        if field == "from_email" and value:
            if op == "contains":
                stmt = stmt.where(Email.from_email.ilike(f"%{value}%"))
            else:
                stmt = stmt.where(Email.from_email.ilike(f"%{value}%"))
        elif field == "to_email" and value:
            # Search in to_emails array
            stmt = stmt.where(func.array_to_string(Email.to_emails, ',').ilike(f"%{value}%"))
        elif field == "labels" and value:
            if isinstance(value, list):
                stmt = stmt.where(Email.labels.overlap(value))
            else:
                stmt = stmt.where(Email.labels.overlap([str(value)]))
        elif field == "has_attachments" and value is not None:
            stmt = stmt.where(Email.has_attachments == bool(value))
        elif field == "date_after" and value:
            stmt = stmt.where(Email.sent_at >= _parse_date_bound(str(value)))
        elif field == "date_before" and value:
            stmt = stmt.where(Email.sent_at <= _parse_date_bound(str(value), end_of_day=True))
        elif field == "account_id" and value is not None:
            stmt = stmt.where(Email.account_id == int(value))

    return stmt


def _email_to_result_dict(
    email: Email,
    label_maps: dict[int, dict[str, str]],
    scores: dict[str, float],
    methods_used: list[str],
) -> dict[str, Any]:
    """Convert an Email ORM object to a SearchResultV1-compatible dict."""
    label_id_to_name = label_maps.get(email.account_id)
    ext = to_external_email(email, PRIVACY_MODE_EXTERNAL, label_id_to_name=label_id_to_name)
    ext_dict = external_email_to_dict(ext)

    from_str = (
        f"{email.from_name} <{email.from_email}>"
        if (email.from_name and email.from_name.strip())
        else (email.from_email or "")
    )

    return {
        "id": email.gmail_id,
        "source": "email",
        "source_class": "personal",
        "title": email.subject or "No subject",
        "snippet": ext_dict.get("snippet", ""),
        "timestamp": email.sent_at.isoformat() if email.sent_at else None,
        "scores": scores,
        "methods_used": methods_used,
        "metadata": {
            "email_id": email.gmail_id,
            "thread_id": email.gmail_thread_id,
            "from": from_str,
            "to": list(email.to_emails or []),
            "labels": ext_dict.get("labels", []),
            "has_attachments": email.has_attachments or False,
            "gmail_url": ext_dict.get("gmail_url", ""),
            "body": ext_dict.get("body", ""),
        },
        "provenance": f"email from {from_str} on {email.sent_at.strftime('%Y-%m-%d') if email.sent_at else '?'}",
    }


from common.search import BaseHybridSearchEngine

class HybridEmailSearchEngine(BaseHybridSearchEngine[Email]):
    """Hybrid search engine for emails using lilith-core."""

    def __init__(self, db: Session, embedder: Embedder) -> None:
        super().__init__(db, embedder)

    def _get_item_id(self, item: Email) -> str:
        return item.gmail_id

    def _structured(self, filters: list[dict] | None, limit: int) -> list[tuple[Email, float]]:
        stmt = select(Email)
        stmt = _apply_filters(stmt, filters, None)
        stmt = stmt.order_by(Email.sent_at.desc().nullslast()).limit(limit)
        rows = self.db.execute(stmt).scalars().all()
        return [(row, max(0.3, 1.0 - i * 0.03)) for i, row in enumerate(rows)]

    def _fulltext(self, query: str, filters: list[dict] | None, limit: int) -> list[tuple[Email, float]]:
        tsquery = func.plainto_tsquery("simple", query)
        rank = func.ts_rank_cd(Email.search_tsv, tsquery)
        stmt = select(Email, rank.label("rank"))
        stmt = _apply_filters(stmt, filters, None)
        stmt = stmt.where(Email.search_tsv.isnot(None))
        stmt = stmt.where(literal_column("search_tsv").op("@@")(tsquery))
        stmt = stmt.order_by(rank.desc(), Email.sent_at.desc().nullslast()).limit(limit)
        rows = self.db.execute(stmt).all()
        results = []
        for row in rows:
            email = row[0]
            ts_rank = float(row[1]) if row[1] is not None else 0.0
            results.append((email, max(0.1, min(1.0, ts_rank))))
        return results

    def _vector(self, query: str, filters: list[dict] | None, limit: int) -> list[tuple[Email, float]]:
        if not self.embedder:
            return []
        query_embedding = self.embedder.encode_sync(query)
        if not query_embedding or not any(x != 0 for x in query_embedding):
            return []
        has_any = (
            Email.subject_embedding.isnot(None)
            | Email.body_embedding.isnot(None)
            | Email.body_pooled_embedding.isnot(None)
        )
        d_body = Email.body_embedding.cosine_distance(query_embedding)
        d_pooled = Email.body_pooled_embedding.cosine_distance(query_embedding)
        d_subj = Email.subject_embedding.cosine_distance(query_embedding)
        distance = func.coalesce(d_body, d_pooled, d_subj)
        stmt = select(Email, distance.label("distance"))
        stmt = _apply_filters(stmt, filters, None)
        stmt = stmt.where(has_any)
        stmt = stmt.order_by(distance, Email.sent_at.desc().nullslast()).limit(limit)
        rows = self.db.execute(stmt).all()
        results = []
        for row in rows:
            email = row[0]
            dist = float(row[1]) if row[1] is not None else 1.0
            results.append((email, max(0.0, min(1.0, 1.0 - dist))))
        return results

    def _format_result(self, item: Email, scores: dict[str, float], methods: list[str]) -> dict[str, Any]:
        # Label maps are handled by a helper in Email
        label_maps = _load_label_maps(self.db, [item.account_id])
        return _email_to_result_dict(item, label_maps, scores, methods)

    # Wrap the search method to maintain the exact same response structure if necessary,
    # though lilith-core's format is very similar.
    def search(self, query: str = "", methods: list[str] | None = None, filters: list[dict] | None = None, account_id: int | None = None, top_k: int = 10) -> dict:
        # For Gmail, we might want to override to handle account_id explicitly in filters
        if account_id is not None:
            filters = (filters or []) + [{"field": "account_id", "value": account_id}]
        
        results, timing_ms, methods_executed = super().search(query, methods, filters, top_k)
        
        return {
            "results": results,
            "total_available": len(results), # Base class doesn't track total across all if limited
            "methods_executed": methods_executed,
            "timing_ms": timing_ms,
            "error": None,
        }

    def _get_item_by_id(self, item_id: str, account_id: int | None = None, **kwargs) -> Email | None:
        stmt = (
            select(Email)
            .where(Email.gmail_id == item_id)
            .where(Email.deleted_at.is_(None))
        )
        stmt = _transformed_only(stmt)
        if account_id is not None:
            stmt = stmt.where(Email.account_id == account_id)
        return self.db.execute(stmt).scalars().one_or_none()

    def get_thread(self, thread_id: str, account_id: int | None = None) -> dict[str, Any] | None:
        stmt = (
            select(Email)
            .where(Email.gmail_thread_id == thread_id)
            .where(Email.deleted_at.is_(None))
            .order_by(Email.sent_at.asc())
        )
        stmt = _transformed_only(stmt)
        if account_id is not None:
            stmt = stmt.where(Email.account_id == account_id)
        rows = self.db.execute(stmt).scalars().all()
        if not rows:
            return None
        account_ids = list({e.account_id for e in rows})
        label_maps = _load_label_maps(self.db, account_ids)
        messages = [
            _email_to_result_dict(e, label_maps, {}, ["thread_lookup"])
            for e in rows
        ]
        return {
            "thread_id": thread_id,
            "subject": rows[0].subject,
            "message_count": len(messages),
            "messages": messages,
        }
