"""HTTP-MCP client implementing the `plsearch` CLI.

Talks to the running plsearch server over streamable-http rather than
embedding Playwright. Two processes can't share the persistent Chrome
profile (single-owner OS lock on `user_data_dir`), so the embedded approach
would conflict with the server — and SessionRegistry would kill it anyway.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import timedelta
from typing import Sequence

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from plsearch.config import get_http_host, get_http_port

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_TRANSPORT_ERROR = 1
EXIT_TOOL_ERROR = 2

# Generous default — Web_search walks multiple Google pages and waits for
# CAPTCHA when one appears. The server-side CAPTCHA wait is 120s; give the
# request a window that covers it plus pagination overhead.
DEFAULT_READ_TIMEOUT_SECONDS = 180.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plsearch",
        description=(
            "Query a running plsearch MCP server over streamable-http. "
            "Defaults to PLSEARCH_HOST/PLSEARCH_PORT (127.0.0.1:8765)."
        ),
    )
    parser.add_argument("query", help="Google search query.")
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of results (default: 10).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Server host (default: PLSEARCH_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Server port (default: PLSEARCH_PORT or 8765).",
    )
    return parser


def _resolve_url(host: str | None, port: int | None) -> str:
    """Build the `/mcp` endpoint URL from CLI flags + config defaults.

    Maps the IPv4 wildcard ``0.0.0.0`` to ``127.0.0.1``: ``0.0.0.0`` is a
    listen-only address — Windows ``connect()`` rejects it with WinError
    10049, and Linux only "works" because the kernel quietly rewrites it to
    loopback. Operators commonly set ``PLSEARCH_HOST=0.0.0.0`` so the
    server binds to all interfaces; the local CLI then inherits the same
    env and would build a broken connect URL. To hit a remote server, pass
    its actual external address via ``--host``.
    """
    resolved_host = host if host is not None else get_http_host()
    resolved_port = port if port is not None else get_http_port()
    if resolved_host == "0.0.0.0":
        resolved_host = "127.0.0.1"
    return f"http://{resolved_host}:{resolved_port}/mcp"


def _unwrap_structured(structured: dict | None) -> object:
    """Strip FastMCP's `{"result": ...}` wrapper added for non-object returns.

    FastMCP wraps tools that return non-dict types (here: `list[dict]`) under a
    `result` key in `structuredContent`. Other keys (or `None`) pass through.
    """
    if structured is None:
        return None
    if list(structured.keys()) == ["result"]:
        return structured["result"]
    return structured


def _format_payload(result) -> str:
    """Pick the most informative payload from a CallToolResult and JSON-format it.

    Prefers `structuredContent` (parsed list/dict). Falls back to the first
    TextContent block's `.text`, parsing it as JSON if possible — this keeps
    output structured even if a server build stops emitting structuredContent.
    """
    payload = _unwrap_structured(getattr(result, "structuredContent", None))
    if payload is None:
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = text
            break
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _call_server(url: str, query: str, limit: int) -> tuple[int, str]:
    """Connect to the server, call Web_search, return (exit_code, text_to_emit).

    Anything user-visible goes through the returned tuple — keeps the network
    code free of `print`/`sys.exit` so it stays testable.
    """
    try:
        async with streamable_http_client(url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "Web_search",
                    {"state": query, "limit": limit},
                    read_timeout_seconds=timedelta(seconds=DEFAULT_READ_TIMEOUT_SECONDS),
                )
    except Exception as exc:
        message = (
            f"plsearch: could not reach MCP server at {url}: {exc}\n"
            f"start it with: uv run python -m plsearch.main"
        )
        return EXIT_TRANSPORT_ERROR, message

    if result.isError:
        message = "plsearch: tool returned an error\n" + _format_payload(result)
        return EXIT_TOOL_ERROR, message

    return EXIT_OK, _format_payload(result)


def _fix_windows_console_encoding() -> None:
    """Force UTF-8 on Windows consoles so non-ASCII titles print without mojibake."""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse argv, talk to the server, print the result, return an exit code."""
    _fix_windows_console_encoding()
    parser = _build_parser()
    args = parser.parse_args(argv)
    url = _resolve_url(args.host, args.port)
    limit = max(1, args.limit)

    exit_code, message = asyncio.run(_call_server(url, args.query, limit))
    stream = sys.stdout if exit_code == EXIT_OK else sys.stderr
    print(message, file=stream)
    return exit_code
