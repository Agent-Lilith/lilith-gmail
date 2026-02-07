import logging
from typing import Any, Dict, List, Optional

from core.config import settings as core_settings
from core.database import db_session
from core.embeddings import Embedder
from mcp_server.email_search import EmailSearchEngine
from mcp_server.summarization import summarize_emails

logger = logging.getLogger(__name__)


def _account_id() -> Optional[int]:
    return core_settings.MCP_EMAIL_ACCOUNT_ID


def search_emails(
    query: str,
    from_email: Optional[str] = None,
    labels: Optional[List[str]] = None,
    has_attachments: Optional[bool] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    try:
        filters: Dict[str, Any] = {}
        if from_email:
            filters["from"] = from_email
        if labels:
            filters["labels"] = labels
        if has_attachments is not None:
            filters["has_attachments"] = has_attachments
        if date_after:
            filters["date_after"] = date_after
        if date_before:
            filters["date_before"] = date_before
        limit = min(max(1, limit), 50)

        with db_session() as db:
            engine = EmailSearchEngine(db, Embedder())
            return engine.search(
                query=query,
                account_id=_account_id(),
                filters=filters,
                limit=limit,
            )
    except Exception as e:
        logger.exception("search_emails failed")
        return [{"error": f"Search failed: {e!s}"}]


def get_email(email_id: str) -> Dict[str, Any]:
    try:
        with db_session() as db:
            engine = EmailSearchEngine(db, Embedder())
            result = engine.get_by_id(email_id, account_id=_account_id())
        if result is None:
            return {"error": "Email not found"}
        return result
    except Exception as e:
        logger.exception("get_email failed")
        return {"error": f"Failed to get email: {e!s}"}


def get_email_thread(thread_id: str) -> Dict[str, Any]:
    try:
        with db_session() as db:
            engine = EmailSearchEngine(db, Embedder())
            result = engine.get_thread(
                thread_id,
                account_id=_account_id(),
            )
        if result is None:
            return {"error": "Thread not found"}
        return result
    except Exception as e:
        logger.exception("get_email_thread failed")
        return {"error": f"Failed to get thread: {e!s}"}


def summarize_emails_tool(
    email_ids: Optional[List[str]] = None,
    thread_id: Optional[str] = None,
) -> str:
    try:
        with db_session() as db:
            engine = EmailSearchEngine(db, Embedder())
            if thread_id:
                data = engine.get_thread(thread_id, account_id=_account_id())
                emails = data["messages"] if data else []
            elif email_ids:
                emails = []
                for eid in email_ids:
                    e = engine.get_by_id(eid, account_id=_account_id())
                    if e:
                        emails.append(e)
            else:
                return "No emails or thread specified. Provide thread_id or email_ids."
        return summarize_emails(emails)
    except Exception as e:
        logger.exception("summarize_emails failed")
        return f"Summarization failed: {e!s}"
