import logging
import sys

from mcp.server.fastmcp import FastMCP

from mcp_server.tools import (
    get_email,
    get_email_thread,
    search_emails,
    summarize_emails_tool,
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
def search_emails_tool(
    query: str,
    from_email: str | None = None,
    labels: list[str] | None = None,
    has_attachments: bool | None = None,
    date_after: str | None = None,
    date_before: str | None = None,
    limit: int = 10,
) -> list[dict]:
    return search_emails(
        query=query,
        from_email=from_email,
        labels=labels,
        has_attachments=has_attachments,
        date_after=date_after,
        date_before=date_before,
        limit=limit,
    )


@mcp.tool()
def get_email_tool(email_id: str) -> dict:
    return get_email(email_id)


@mcp.tool()
def get_email_thread_tool(thread_id: str) -> dict:
    return get_email_thread(thread_id)


@mcp.tool()
def summarize_emails(
    email_ids: list[str] | None = None,
    thread_id: str | None = None,
) -> str:
    return summarize_emails_tool(email_ids=email_ids, thread_id=thread_id)


def main(
    transport: str = "stdio",
    port: int = 8001,
) -> int:
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        app = _create_mcp(host="0.0.0.0", port=port)
        app.tool()(search_emails_tool)
        app.tool()(get_email_tool)
        app.tool()(get_email_thread_tool)
        app.tool()(summarize_emails)
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
