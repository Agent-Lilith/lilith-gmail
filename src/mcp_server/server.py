import logging
import sys

from common.mcp import create_mcp_app, run_mcp_server

from mcp_server.tools import (
    get_email_thread_tool,
    get_email_tool,
    get_search_capabilities_tool,
    search_emails_unified_tool,
    summarize_emails_tool,
)

logger = logging.getLogger("mcp_server")

mcp = create_mcp_app("Lilith Email")


@mcp.tool()
def search_capabilities() -> dict:
    """Return this server's search capabilities."""
    return get_search_capabilities_tool()


@mcp.tool()
def unified_search(
    query: str = "",
    methods: list[str] | None = None,
    filters: list[dict] | None = None,
    top_k: int = 10,
    account_id: int | None = None,
) -> dict:
    """Hybrid search over emails (structured + fulltext + vector)."""
    return search_emails_unified_tool(
        query=query,
        methods=methods,
        filters=filters,
        top_k=top_k,
        account_id=account_id,
    )


@mcp.tool()
def email_get(email_id: str, account_id: int | None = None) -> dict:
    """Get a single email by Gmail message ID."""
    return get_email_tool(email_id, account_id=account_id)


@mcp.tool()
def email_get_thread(thread_id: str, account_id: int | None = None) -> dict:
    """Get all emails in a thread by Gmail thread ID."""
    return get_email_thread_tool(thread_id, account_id=account_id)


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


def main(transport: str | None = None, port: int | None = None) -> int:
    import argparse

    if transport is None or port is None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--transport", default="stdio")
        parser.add_argument("--port", type=int, default=8001)
        args, _ = parser.parse_known_args()
        transport = transport if transport is not None else args.transport
        port = port if port is not None else args.port
    run_mcp_server(mcp, transport=transport, port=port)
    return 0


if __name__ == "__main__":
    from mcp_server.__main__ import main as _main

    sys.exit(_main())
