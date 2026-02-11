"""Lilith Email MCP server: hybrid search (structured + fulltext + vector), get, thread, summarize."""

import json
import logging
import sys

from mcp.server.fastmcp import FastMCP

from mcp_server.tools import (
    get_email,
    get_email_thread,
    search_emails_unified,
    summarize_emails_tool,
    get_search_capabilities,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_server")


def _create_mcp(host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    return FastMCP(
        "Lilith Email",
        json_response=True,
        host=host,
        port=port,
    )


mcp = _create_mcp()


@mcp.tool()
def search_capabilities() -> dict:
    """Return this server's search capabilities: supported methods, filters, limits."""
    return get_search_capabilities()


@mcp.tool()
def unified_search(
    query: str = "",
    methods: list[str] | None = None,
    filters: list[dict] | None = None,
    top_k: int = 10,
    include_scores: bool = True,
    account_id: int | None = None,
) -> dict:
    """Hybrid search over emails using structured, fulltext, and/or vector methods.

    Args:
        query: Semantic or keyword query. Empty for structured-only search.
        methods: List of retrieval methods to use: 'structured', 'fulltext', 'vector'.
                 None = auto-select based on query and filters.
        filters: List of filter clauses: [{"field": "from_email", "operator": "contains", "value": "john"}].
                 Supported fields: from_email, to_email, labels, has_attachments, date_after, date_before, account_id.
        top_k: Maximum results to return (1-100).
        include_scores: Whether to include per-method scores in results.
        account_id: Restrict to this email account. None = use default.

    Returns:
        {results: [...], total_available, methods_executed, timing_ms, error}
    """
    return search_emails_unified(
        query=query,
        methods=methods,
        filters=filters,
        top_k=top_k,
        include_scores=include_scores,
        account_id=account_id,
    )


@mcp.tool()
def email_get(email_id: str, account_id: int | None = None) -> dict:
    """Get a single email by Gmail message ID."""
    return get_email(email_id, account_id=account_id)


@mcp.tool()
def email_get_thread(thread_id: str, account_id: int | None = None) -> dict:
    """Get all emails in a thread by Gmail thread ID."""
    return get_email_thread(thread_id, account_id=account_id)


@mcp.tool()
def emails_summarize(
    email_ids: list[str] | None = None,
    thread_id: str | None = None,
    account_id: int | None = None,
) -> dict:
    """Summarize one or more emails or a full thread."""
    return summarize_emails_tool(
        email_ids=email_ids,
        thread_id=thread_id,
        account_id=account_id,
    )


def main(
    transport: str = "stdio",
    port: int = 8001,
) -> int:
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        app = _create_mcp(host="0.0.0.0", port=port)
        app.tool()(search_capabilities)
        app.tool()(unified_search)
        app.tool()(email_get)
        app.tool()(email_get_thread)
        app.tool()(emails_summarize)
        import asyncio
        import uvicorn
        from contextlib import asynccontextmanager
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.cors import CORSMiddleware
        from starlette.routing import Mount

        starlette_app = app.streamable_http_app()
        session_manager = app.session_manager

        async def _rewrite_root_to_mcp(scope, receive, send):
            if scope.get("path") == "/":
                scope = {**scope, "path": "/mcp"}
            await starlette_app(scope, receive, send)

        @asynccontextmanager
        async def lifespan(asgi_app):
            async with session_manager.run():
                yield

        cors_app = Starlette(
            routes=[Mount("/", app=_rewrite_root_to_mcp)],
            middleware=[
                Middleware(
                    CORSMiddleware,
                    allow_origins=["*"],
                    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
                    allow_headers=["*"],
                    expose_headers=["*"],
                )
            ],
            lifespan=lifespan,
        )
        config = uvicorn.Config(
            cors_app,
            host="0.0.0.0",
            port=port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        asyncio.run(server.serve())
    return 0


if __name__ == "__main__":
    from mcp_server.__main__ import main as _main
    sys.exit(_main())
