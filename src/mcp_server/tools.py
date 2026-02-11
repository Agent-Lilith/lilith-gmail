"""Email MCP tools: hybrid search, get, thread, summarize, capabilities.

Return shape (ToolResult): { "success": true, "output": str } or { "success": false, "error": str }.
Sync implementations; callers must run in thread pool to avoid blocking.
"""

import json
import logging
from typing import Any

from core.config import settings as core_settings
from core.database import db_session
from core.embeddings import Embedder
from mcp_server.hybrid_search import HybridEmailSearchEngine
from mcp_server.summarization import summarize_emails

logger = logging.getLogger(__name__)

ToolResult = dict[str, Any]


def _resolve_account_id(account_id: int | None) -> int | None:
    return account_id if account_id is not None else core_settings.MCP_EMAIL_ACCOUNT_ID


def get_search_capabilities() -> dict[str, Any]:
    """Return capabilities for this email search server."""
    return {
        "schema_version": "1.0",
        "source_name": "email",
        "source_class": "personal",
        "supported_methods": ["structured", "fulltext", "vector"],
        "supported_filters": [
            {"name": "from_email", "type": "string", "operators": ["eq", "contains"], "description": "Sender email address"},
            {"name": "to_email", "type": "string", "operators": ["eq", "contains"], "description": "Recipient email address"},
            {"name": "labels", "type": "string[]", "operators": ["in"], "description": "Gmail label IDs or names"},
            {"name": "has_attachments", "type": "boolean", "operators": ["eq"], "description": "Has attachments filter"},
            {"name": "date_after", "type": "date", "operators": ["gte"], "description": "Emails sent on or after this date (ISO format)"},
            {"name": "date_before", "type": "date", "operators": ["lte"], "description": "Emails sent on or before this date (ISO format)"},
            {"name": "account_id", "type": "integer", "operators": ["eq"], "description": "Restrict to specific email account"},
        ],
        "max_limit": 100,
        "default_limit": 10,
        "sort_fields": ["sent_at", "relevance"],
        "default_ranking": "vector",
    }


def search_emails_unified(
    query: str = "",
    methods: list[str] | None = None,
    filters: list[dict] | None = None,
    top_k: int = 10,
    include_scores: bool = True,
    account_id: int | None = None,
) -> ToolResult:
    """Hybrid search over emails."""
    try:
        aid = _resolve_account_id(account_id)
        top_k = min(max(1, top_k), 100)

        with db_session() as db:
            engine = HybridEmailSearchEngine(db, Embedder())
            response = engine.search(
                query=query,
                methods=methods,
                filters=filters,
                account_id=aid,
                top_k=top_k,
                include_scores=include_scores,
            )

        return {"success": True, "output": json.dumps(response)}
    except Exception as e:
        logger.exception("unified_search failed")
        return {"success": False, "error": f"Search failed: {e!s}"}


def get_email(email_id: str, account_id: int | None = None) -> ToolResult:
    try:
        with db_session() as db:
            engine = HybridEmailSearchEngine(db, Embedder())
            result = engine.get_by_id(email_id, account_id=_resolve_account_id(account_id))
        if result is None:
            return {"success": False, "error": "Email not found"}
        return {"success": True, "output": json.dumps(result)}
    except Exception as e:
        logger.exception("get_email failed")
        return {"success": False, "error": f"Failed to get email: {e!s}"}


def get_email_thread(thread_id: str, account_id: int | None = None) -> ToolResult:
    try:
        with db_session() as db:
            engine = HybridEmailSearchEngine(db, Embedder())
            result = engine.get_thread(thread_id, account_id=_resolve_account_id(account_id))
        if result is None:
            return {"success": False, "error": "Thread not found"}
        return {"success": True, "output": json.dumps(result)}
    except Exception as e:
        logger.exception("get_email_thread failed")
        return {"success": False, "error": f"Failed to get thread: {e!s}"}


def summarize_emails_tool(
    email_ids: list[str] | None = None,
    thread_id: str | None = None,
    account_id: int | None = None,
) -> ToolResult:
    try:
        aid = _resolve_account_id(account_id)
        with db_session() as db:
            engine = HybridEmailSearchEngine(db, Embedder())
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
