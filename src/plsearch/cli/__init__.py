"""Thin HTTP-MCP client for the plsearch server.

Use `uv run plsearch "query"` or `python -m plsearch.cli "query"` to call the
already-running server (defaults: 127.0.0.1:8765/mcp). Does NOT spin up its
own Playwright — that would fight the server for the persistent profile lock.
"""

from plsearch.cli.client import main

__all__ = ["main"]
