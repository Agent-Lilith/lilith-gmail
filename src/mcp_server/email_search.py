from datetime import date, datetime, time
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.embeddings import Embedder
from core.models import AccountLabel, Email

from .external_format import external_email_to_dict, to_external_email

PRIVACY_MODE_EXTERNAL = "external"


def _parse_date_bound(s: str, end_of_day: bool = False) -> datetime:
    """Parse date_after/date_before string to datetime for DB comparison."""
    s = s.strip()
    if "T" in s or " " in s:
        # ISO datetime; normalize Z to +00:00 for fromisoformat
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    d = date.fromisoformat(s)
    if end_of_day:
        return datetime.combine(d, time(23, 59, 59, 999999))
    return datetime.combine(d, time(0, 0, 0))


def _transformed_only(stmt):
    return stmt.where(Email.transform_completed_at.isnot(None))


def _load_label_maps(db: Session, account_ids: List[int]) -> Dict[int, Dict[str, str]]:
    if not account_ids:
        return {}
    rows = db.execute(
        select(AccountLabel).where(AccountLabel.account_id.in_(account_ids))
    ).scalars().all()
    out: Dict[int, Dict[str, str]] = {aid: {} for aid in account_ids}
    for al in rows:
        out[al.account_id][al.label_id] = al.label_name
    return out


class EmailSearchEngine:
    def __init__(self, db: Session, embedder: Embedder) -> None:
        self.db = db
        self.embedder = embedder

    def search(
        self,
        query: str,
        account_id: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 10,
        privacy_mode: str = PRIVACY_MODE_EXTERNAL,
    ) -> List[Dict[str, Any]]:
        filters = filters or {}
        stmt = select(Email).where(Email.deleted_at.is_(None))
        stmt = _transformed_only(stmt)

        if account_id is not None:
            stmt = stmt.where(Email.account_id == account_id)

        if filters.get("from"):
            stmt = stmt.where(Email.from_email.ilike(f"%{filters['from']}%"))
        if filters.get("labels"):
            stmt = stmt.where(Email.labels.overlap(filters["labels"]))
        if "has_attachments" in filters:
            stmt = stmt.where(Email.has_attachments == filters["has_attachments"])
        if filters.get("date_after"):
            stmt = stmt.where(Email.sent_at >= _parse_date_bound(filters["date_after"]))
        if filters.get("date_before"):
            stmt = stmt.where(
                Email.sent_at <= _parse_date_bound(filters["date_before"], end_of_day=True)
            )

        query_embedding = self.embedder.encode_sync(query)
        if query_embedding and any(x != 0 for x in query_embedding):
            has_any = (
                Email.subject_embedding.isnot(None)
                | Email.body_embedding.isnot(None)
                | Email.body_pooled_embedding.isnot(None)
            )
            stmt = stmt.where(has_any)
            d_body = Email.body_embedding.cosine_distance(query_embedding)
            d_pooled = Email.body_pooled_embedding.cosine_distance(query_embedding)
            d_subj = Email.subject_embedding.cosine_distance(query_embedding)
            stmt = stmt.order_by(func.coalesce(d_body, d_pooled, d_subj))
        else:
            stmt = stmt.order_by(Email.sent_at.desc().nullslast())

        stmt = stmt.limit(limit)
        rows = self.db.execute(stmt).scalars().all()
        account_ids = list({e.account_id for e in rows})
        label_maps = _load_label_maps(self.db, account_ids)
        return [
            external_email_to_dict(
                to_external_email(
                    e,
                    privacy_mode,
                    label_id_to_name=label_maps.get(e.account_id),
                )
            )
            for e in rows
        ]

    def get_by_id(
        self,
        gmail_id: str,
        account_id: Optional[int] = None,
        privacy_mode: str = PRIVACY_MODE_EXTERNAL,
    ) -> Optional[Dict[str, Any]]:
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
            to_external_email(
                email,
                privacy_mode,
                label_id_to_name=label_maps.get(email.account_id),
            )
        )

    def get_thread(
        self,
        thread_id: str,
        account_id: Optional[int] = None,
        privacy_mode: str = PRIVACY_MODE_EXTERNAL,
    ) -> Optional[Dict[str, Any]]:
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
                to_external_email(
                    e,
                    privacy_mode,
                    label_id_to_name=label_maps.get(e.account_id),
                )
            )
            for e in rows
        ]
        return {
            "thread_id": thread_id,
            "subject": rows[0].subject,
            "message_count": len(messages),
            "messages": messages,
        }
