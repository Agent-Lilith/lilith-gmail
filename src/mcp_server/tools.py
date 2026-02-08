"""Email tools for Lilith / MCP.

Return shape (ToolResult): On success {"success": True, "output": str}. On failure {"success": False, "error": str}.
No error payload inside output; errors only in error field.

Every tool accepts optional account_id: int | None. If omitted, uses MCP_EMAIL_ACCOUNT_ID.

Sync implementations; callers must run in thread pool (e.g. asyncio.to_thread) to avoid blocking.

Interface:
  emails_search       Params: query, from_email?, labels?, has_attachments?, date_after?, date_before?, limit?, account_id?
    Success: output = JSON array of email dicts. Error: e.g. "Search failed: ..."
  email_get           Params: email_id, account_id?
    Success: output = JSON object. Error: "Email not found" or "Failed to get email: ..."
  email_get_thread    Params: thread_id, account_id?
    Success: output = JSON {thread_id, subject, message_count, messages}. Error: "Thread not found" or "..."
  emails_summarize    Params: email_ids?, thread_id?, account_id?
    Success: output = human-readable summary text. Error: "No emails or thread specified..." or "Summarization failed: ..."
"""
import json
import logging
from typing import Any, Dict, List, Optional

from core.config import settings as core_settings
from core.database import db_session
from core.embeddings import Embedder
from mcp_server.email_search import EmailSearchEngine
from mcp_server.summarization import summarize_emails

logger = logging.getLogger(__name__)

ToolResult = Dict[str, Any]


def _resolve_account_id(account_id: Optional[int]) -> Optional[int]:
    return account_id if account_id is not None else core_settings.MCP_EMAIL_ACCOUNT_ID


def search_emails(
    query: str,
    from_email: Optional[str] = None,
    labels: Optional[List[str]] = None,
    has_attachments: Optional[bool] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    limit: int = 10,
    account_id: Optional[int] = None,
) -> ToolResult:
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
            results = engine.search(
                query=query,
                account_id=_resolve_account_id(account_id),
                filters=filters,
                limit=limit,
            )
        return {"success": True, "output": json.dumps(results)}
    except Exception as e:
        logger.exception("search_emails failed")
        return {"success": False, "error": f"Search failed: {e!s}"}


def get_email(
    email_id: str,
    account_id: Optional[int] = None,
) -> ToolResult:
    try:
        with db_session() as db:
            engine = EmailSearchEngine(db, Embedder())
            result = engine.get_by_id(
                email_id,
                account_id=_resolve_account_id(account_id),
            )
        if result is None:
            return {"success": False, "error": "Email not found"}
        return {"success": True, "output": json.dumps(result)}
    except Exception as e:
        logger.exception("get_email failed")
        return {"success": False, "error": f"Failed to get email: {e!s}"}


def get_email_thread(
    thread_id: str,
    account_id: Optional[int] = None,
) -> ToolResult:
    try:
        with db_session() as db:
            engine = EmailSearchEngine(db, Embedder())
            result = engine.get_thread(
                thread_id,
                account_id=_resolve_account_id(account_id),
            )
        if result is None:
            return {"success": False, "error": "Thread not found"}
        return {"success": True, "output": json.dumps(result)}
    except Exception as e:
        logger.exception("get_email_thread failed")
        return {"success": False, "error": f"Failed to get thread: {e!s}"}


def summarize_emails_tool(
    email_ids: Optional[List[str]] = None,
    thread_id: Optional[str] = None,
    account_id: Optional[int] = None,
) -> ToolResult:
    try:
        aid = _resolve_account_id(account_id)
        with db_session() as db:
            engine = EmailSearchEngine(db, Embedder())
            if thread_id:
                data = engine.get_thread(thread_id, account_id=aid)
                emails = data["messages"] if data else []
            elif email_ids:
                emails = []
                for eid in email_ids:
                    e = engine.get_by_id(eid, account_id=aid)
                    if e:
                        emails.append(e)
            else:
                return {"success": False, "error": "No emails or thread specified. Provide thread_id or email_ids."}
        summary = summarize_emails(emails)
        return {"success": True, "output": summary}
    except Exception as e:
        logger.exception("summarize_emails failed")
        return {"success": False, "error": f"Summarization failed: {e!s}"}
