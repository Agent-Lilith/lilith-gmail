import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="mcp", description="Lilith Email MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport: stdio (default) or streamable-http",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port for streamable-http (default: 8001)",
    )
    args = parser.parse_args()

    if args.transport == "streamable-http":
        os.environ["FASTMCP_PORT"] = str(args.port)
        os.environ["FASTMCP_HOST"] = "0.0.0.0"

    from mcp_server.server import main as server_main

    return server_main(transport=args.transport, port=args.port)


if __name__ == "__main__":
    sys.exit(main())
