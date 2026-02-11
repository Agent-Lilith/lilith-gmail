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


class HybridEmailSearchEngine:
    """Runs structured, fulltext, and vector search in parallel, merges results."""

    def __init__(self, db: Session, embedder: Embedder) -> None:
        self.db = db
        self.embedder = embedder

    def search(
        self,
        query: str = "",
        methods: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        account_id: int | None = None,
        top_k: int = 10,
        include_scores: bool = True,
    ) -> dict[str, Any]:
        """Execute hybrid search and return UnifiedSearchResponse-compatible dict."""
        top_k = min(max(1, top_k), 100)

        # Auto-select methods if not specified
        if methods is None:
            methods = self._auto_select_methods(query, filters)

        logger.info(
            "HybridEmailSearch: methods=%s, query=%r, filters=%s, top_k=%s",
            methods, query[:80] if query else "", len(filters or []), top_k,
        )

        all_results: dict[str, dict[str, Any]] = {}  # gmail_id -> result
        methods_executed: list[str] = []
        timing_ms: dict[str, float] = {}

        if "structured" in methods and filters:
            t0 = time.monotonic()
            structured_results = self._structured_search(filters, account_id, top_k)
            timing_ms["structured"] = round((time.monotonic() - t0) * 1000, 1)
            methods_executed.append("structured")
            for email, score in structured_results:
                gid = email.gmail_id
                if gid not in all_results:
                    all_results[gid] = {"email": email, "scores": {}, "methods": []}
                all_results[gid]["scores"]["structured"] = score
                all_results[gid]["methods"].append("structured")

        if "fulltext" in methods and query.strip():
            t0 = time.monotonic()
            fulltext_results = self._fulltext_search(query, filters, account_id, top_k)
            timing_ms["fulltext"] = round((time.monotonic() - t0) * 1000, 1)
            methods_executed.append("fulltext")
            for email, score in fulltext_results:
                gid = email.gmail_id
                if gid not in all_results:
                    all_results[gid] = {"email": email, "scores": {}, "methods": []}
                all_results[gid]["scores"]["fulltext"] = score
                if "fulltext" not in all_results[gid]["methods"]:
                    all_results[gid]["methods"].append("fulltext")

        if "vector" in methods and query.strip():
            t0 = time.monotonic()
            vector_results = self._vector_search(query, filters, account_id, top_k)
            timing_ms["vector"] = round((time.monotonic() - t0) * 1000, 1)
            methods_executed.append("vector")
            for email, score in vector_results:
                gid = email.gmail_id
                if gid not in all_results:
                    all_results[gid] = {"email": email, "scores": {}, "methods": []}
                all_results[gid]["scores"]["vector"] = score
                if "vector" not in all_results[gid]["methods"]:
                    all_results[gid]["methods"].append("vector")

        # Build label maps for all results
        account_ids = list({r["email"].account_id for r in all_results.values()})
        label_maps = _load_label_maps(self.db, account_ids)

        # Score fusion: weighted aggregate
        weights = {"structured": 1.0, "fulltext": 0.85, "vector": 0.7}
        scored_results: list[tuple[float, dict[str, Any]]] = []
        for gid, data in all_results.items():
            total_w = 0.0
            total_s = 0.0
            for method, score in data["scores"].items():
                w = weights.get(method, 0.5)
                total_w += w
                total_s += score * w
            final = total_s / total_w if total_w > 0 else 0.0

            result_dict = _email_to_result_dict(
                data["email"], label_maps, data["scores"], data["methods"],
            )
            scored_results.append((final, result_dict))

        # Sort by fused score descending
        scored_results.sort(key=lambda x: -x[0])
        results = [r for _, r in scored_results[:top_k]]

        logger.info(
            "HybridEmailSearch complete: %s results, methods=%s, timing=%s",
            len(results), methods_executed, timing_ms,
        )

        return {
            "results": results,
            "total_available": len(all_results),
            "methods_executed": methods_executed,
            "timing_ms": timing_ms,
            "error": None,
        }

    def _auto_select_methods(self, query: str, filters: list[dict] | None) -> list[str]:
        """Determine which methods to run based on query and filters."""
        methods = []
        has_filters = bool(filters)
        has_query = bool(query and query.strip())

        if has_filters:
            methods.append("structured")
        if has_query:
            methods.append("fulltext")
            methods.append("vector")

        # Fallback: if nothing selected, do vector with a generic recent query
        if not methods:
            methods = ["structured"]

        return methods

    def _structured_search(
        self,
        filters: list[dict[str, Any]] | None,
        account_id: int | None,
        limit: int,
    ) -> list[tuple[Email, float]]:
        """Pure SQL filter search, ordered by recency."""
        stmt = select(Email)
        stmt = _apply_filters(stmt, filters, account_id)
        stmt = stmt.order_by(Email.sent_at.desc().nullslast()).limit(limit)

        rows = self.db.execute(stmt).scalars().all()
        # Score by position (exact match gets high score)
        return [(row, max(0.3, 1.0 - i * 0.03)) for i, row in enumerate(rows)]

    def _fulltext_search(
        self,
        query: str,
        filters: list[dict[str, Any]] | None,
        account_id: int | None,
        limit: int,
    ) -> list[tuple[Email, float]]:
        """PostgreSQL tsvector fulltext search with ts_rank_cd scoring."""
        tsquery = func.plainto_tsquery("simple", query)
        rank = func.ts_rank_cd(Email.search_tsv, tsquery)

        stmt = select(Email, rank.label("rank"))
        stmt = _apply_filters(stmt, filters, account_id)
        stmt = stmt.where(Email.search_tsv.isnot(None))
        stmt = stmt.where(literal_column("search_tsv").op("@@")(tsquery))
        stmt = stmt.order_by(rank.desc(), Email.sent_at.desc().nullslast())
        stmt = stmt.limit(limit)

        rows = self.db.execute(stmt).all()
        results = []
        for row in rows:
            email = row[0]
            ts_rank = float(row[1]) if row[1] is not None else 0.0
            # Normalize ts_rank_cd to 0-1 range (it can exceed 1, cap it)
            normalized = min(1.0, ts_rank)
            results.append((email, max(0.1, normalized)))
        return results

    def _vector_search(
        self,
        query: str,
        filters: list[dict[str, Any]] | None,
        account_id: int | None,
        limit: int,
    ) -> list[tuple[Email, float]]:
        """pgvector cosine similarity search."""
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
        stmt = _apply_filters(stmt, filters, account_id)
        stmt = stmt.where(has_any)
        stmt = stmt.order_by(distance, Email.sent_at.desc().nullslast())
        stmt = stmt.limit(limit)

        rows = self.db.execute(stmt).all()
        results = []
        for row in rows:
            email = row[0]
            dist = float(row[1]) if row[1] is not None else 1.0
            score = max(0.0, min(1.0, 1.0 - dist))
            results.append((email, score))
        return results

    # --- Legacy-compatible search for existing email_get/thread tools ---

    def get_by_id(self, gmail_id: str, account_id: int | None = None) -> dict[str, Any] | None:
        stmt = (
            select(Email)
            .where(Email.gmail_id == gmail_id)
            .where(Email.deleted_at.is_(None))
        )
        stmt = _transformed_only(stmt)
        if account_id is not None:
            stmt = stmt.where(Email.account_id == account_id)
        email = self.db.execute(stmt).scalars().one_or_none()
        if not email:
            return None
        label_maps = _load_label_maps(self.db, [email.account_id])
        return external_email_to_dict(
            to_external_email(email, PRIVACY_MODE_EXTERNAL, label_id_to_name=label_maps.get(email.account_id))
        )

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
            external_email_to_dict(
                to_external_email(e, PRIVACY_MODE_EXTERNAL, label_id_to_name=label_maps.get(e.account_id))
            )
            for e in rows
        ]
        return {
            "thread_id": thread_id,
            "subject": rows[0].subject,
            "message_count": len(messages),
            "messages": messages,
        }
