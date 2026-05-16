from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote
import atexit
import logging
import logging.handlers
import os
import signal
import sys

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Playwright, async_playwright

load_dotenv()

from plsearch.parse_page import parse_page
from plsearch.config import (
    GOOGLE_SEARCH_URL,
    LOG_DIR,
    LOG_FILE,
    MAX_PAGES,
    RESULTS_PER_PAGE,
    cleanup_stale_profile_locks,
    get_profile_path,
    is_captcha_page,
    wait_until_captcha_solved,
)
from plsearch.session import SessionRegistry

CAPTCHA_WAIT_TIMEOUT_SECONDS = 120.0
DEFAULT_LIMIT = 10

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from mcp.server.fastmcp import Context, FastMCP


class LineBufferedStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes on every record"""
    def emit(self, record):
        super().emit(record)
        self.flush()


def _running_under_mcp_client() -> bool:
    """True when our parent is feeding us JSON-RPC (stdin is a pipe, not a TTY).

    MCP Inspector (issue #654, still open as of 0.21.2) forwards every line of
    our stderr to the browser via SSE. After a Reconnect the SSE side is dead
    but the proxy doesn't know it; the next stderr write throws "Not connected"
    uncaught and the whole Node proxy crashes. Detecting "we're under a client"
    lets us route logs to the file *only* in that mode, eliminating the trigger.
    """
    try:
        return not sys.stdin.isatty()
    except (AttributeError, OSError):
        return True


def _configure_logging() -> None:
    """Configure root logging for the MCP server process.

    Called from `main()` so that importing `plsearch.main` (e.g. from tests)
    does not reconfigure the root logger as a side effect.

    Under an MCP client the stderr handler is dropped — see
    ``_running_under_mcp_client`` for the reasoning. Logs always go to the
    rotating file, so ``tail -f src/logs/search.log`` is the way to watch
    live output during stdio sessions.

    FastMCP installs a ``RichHandler`` on the root logger when its module is
    imported, before we get here. ``basicConfig`` is a no-op once handlers
    exist, so we explicitly clear root first.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if not _running_under_mcp_client():
        stream_handler = LineBufferedStreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    root.setLevel(logging.DEBUG)


logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Manages a single persistent Chrome context, swapping headless<->headed for CAPTCHA.

    Playwright's `headless` is a launch-time flag — you can't toggle it on a running
    browser. The persistent profile (`user_data_dir`) also locks the directory, so
    only one browser can use it at a time. The swap pattern below closes the
    current browser before relaunching in the other mode; cookies/session state
    persist on disk between swaps via `user_data_dir`.
    """

    playwright: Playwright
    user_data_dir: str
    _browser: BrowserContext | None = None

    async def get_browser(self) -> BrowserContext:
        """Return the cached browser, lazily launching headless if there is none."""
        if self._browser is None:
            self._browser = await self._launch(headless=True)
        return self._browser

    async def reveal_for_captcha(self) -> BrowserContext:
        """Close the current browser, relaunch headed so the user can see/solve CAPTCHA."""
        await self._close_browser()
        self._browser = await self._launch(headless=False)
        return self._browser

    async def hide_after_captcha(self) -> None:
        """Close the headed browser, relaunch headless so the next call is invisible again."""
        await self._close_browser()
        self._browser = await self._launch(headless=True)

    async def shutdown(self) -> None:
        await self._close_browser()

    async def _close_browser(self) -> None:
        if self._browser is not None:
            browser, self._browser = self._browser, None
            await browser.close()

    async def _launch(self, *, headless: bool) -> BrowserContext:
        logger.info("Launching Chrome (headless=%s)", headless)
        return await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            channel="chrome",
            headless=headless,
        )


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Start Playwright at server boot; launch Chrome lazily on first search.

    Chrome is NOT launched here — `AppContext.get_browser()` does it on demand.
    Eager launch caused `TargetClosedError` on discovery-only connections
    (MCP Inspector probes, rapid `/mcp` reconnects): the client closes stdio
    before `launch_persistent_context` returns, the lifespan is cancelled
    mid-launch, and Playwright kills the half-launched Chrome process. Lazy
    launch shifts ~1-2s of startup to the first real `Web_search` call.
    """
    user_data_dir = get_profile_path()
    # Kill any prior server still holding the profile (MCP Inspector restart,
    # Node-proxy crash, Task Manager kill all bypass our finalizer on Windows).
    # The registry's claim() walks the prior PID's process tree, taking Chrome
    # with it; the locks sweep then clears the filesystem turds Chrome leaves
    # when force-killed. Both must happen *before* we touch Playwright.
    registry = SessionRegistry(user_data_dir)
    registry.claim()
    cleanup_stale_profile_locks(user_data_dir)

    logger.info("Lifespan: starting Playwright (profile=%s)", user_data_dir)
    playwright = await async_playwright().start()
    app = AppContext(playwright=playwright, user_data_dir=user_data_dir)
    try:
        yield app
    finally:
        logger.info("Lifespan: shutting down")
        try:
            await app.shutdown()
        finally:
            try:
                await playwright.stop()
            finally:
                cleanup_stale_profile_locks(user_data_dir)
                registry.release()


mcp = FastMCP("Web_search", lifespan=lifespan)


async def _search(
    browser: BrowserContext,
    query: str,
    limit: int,
    *,
    wait_for_captcha: bool,
) -> list[dict] | None:
    """Walk Google result pages until ``limit`` results are collected.

    Uses Google's ``&start=N`` pagination on a single tab. Stops as soon as
    ``limit`` results are gathered, a page yields zero results, or
    ``MAX_PAGES`` pages have been visited.

    CAPTCHA handling depends on where it appears:
      - On the first page with no results collected: returns ``None`` so the
        caller can swap to a visible browser and retry from the top.
      - On a later page: returns the partial results already collected
        (best-effort) when ``wait_for_captcha`` is False.
      - At any page: blocks on user resolution when ``wait_for_captcha`` is
        True; raises ``RuntimeError`` on timeout.
    """
    collected: list[dict] = []
    page = await browser.new_page()
    try:
        logger.info("Searching %r (limit=%d)", query, limit)
        for page_idx in range(MAX_PAGES):
            if len(collected) >= limit:
                break
            offset = page_idx * RESULTS_PER_PAGE
            url = f"{GOOGLE_SEARCH_URL}{quote(query)}&start={offset}"
            await page.goto(url)
            logger.debug("Loaded page %d (start=%d): %s", page_idx + 1, offset, page.url)

            page_content = await page.content()
            if is_captcha_page(page_content):
                if not wait_for_captcha:
                    if collected:
                        logger.warning(
                            "CAPTCHA on page %d — returning %d partial results",
                            page_idx + 1, len(collected),
                        )
                        return collected[:limit]
                    logger.warning("CAPTCHA on first page — will retry in visible browser")
                    return None
                logger.warning(
                    "CAPTCHA on page %d — waiting for user to solve...", page_idx + 1
                )
                solved = await wait_until_captcha_solved(
                    page, timeout=CAPTCHA_WAIT_TIMEOUT_SECONDS
                )
                if not solved:
                    raise RuntimeError(
                        f"CAPTCHA was not solved within {CAPTCHA_WAIT_TIMEOUT_SECONDS}s"
                    )
                logger.info("CAPTCHA solved, continuing walk")

            await page.wait_for_load_state("domcontentloaded")
            page_content = await page.content()
            page_results = parse_page(page_content)
            if not page_results:
                logger.info("Page %d returned no results — stopping walk", page_idx + 1)
                break
            collected.extend(page_results)

        results = collected[:limit]
        logger.info("Collected %d results (asked %d)", len(results), limit)
        return results
    finally:
        await page.close()


async def run(app: AppContext, query: str, limit: int) -> list[dict]:
    """Headless-first multi-page search; falls back to visible Chrome on CAPTCHA."""
    browser = await app.get_browser()
    results = await _search(browser, query, limit, wait_for_captcha=False)
    if results is not None:
        return results

    # Headless hit CAPTCHA on the first page with nothing in hand. Swap to a
    # visible Chrome with the same profile, let the user solve, then always
    # swap back to headless for the next call.
    try:
        headed = await app.reveal_for_captcha()
        return await _search(headed, query, limit, wait_for_captcha=True)
    finally:
        await app.hide_after_captcha()


@mcp.tool()
async def Web_search(state: str, ctx: Context, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Make google search query.

    Walks Google's pagination (start=0, 10, 20, ...) until ``limit`` results
    are collected or pages run out. The walker tops out at MAX_PAGES * ~10
    results regardless of the requested limit.

    Args:
        state: Clear google search query.
        limit: Maximum number of results to return. Defaults to 10 (one page).
    """
    limit = max(1, limit)
    logger.info("Starting MCP search request: %s (limit=%d)", state, limit)
    try:
        app = ctx.request_context.lifespan_context
        results = await run(app, state, limit)
        logger.info("Search request completed successfully (%d results)", len(results))
        return results
    except Exception:
        logger.exception("Error during search request")
        raise


def _install_process_cleanup(user_data_dir: str) -> None:
    """Best-effort cleanup for clean exits (Ctrl+C, SIGTERM, normal return).

    The lifespan finalizer is the primary cleanup path; this is a safety net
    for the narrow window between process start and the lifespan opening, and
    for signal-based exits that bypass the finalizer. Hard kills
    (TerminateProcess on Windows, SIGKILL on Unix) skip all of this —
    SessionRegistry's startup claim is the recovery for those.
    """
    registry = SessionRegistry(user_data_dir)

    def _cleanup() -> None:
        try:
            cleanup_stale_profile_locks(user_data_dir)
        except Exception:
            logger.exception("atexit lock cleanup failed")
        try:
            registry.release()
        except Exception:
            logger.exception("atexit registry release failed")

    atexit.register(_cleanup)

    def _on_signal(signum: int, _frame) -> None:
        logger.info("Received signal %d — exiting", signum)
        sys.exit(0)

    candidates = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        candidates.append(signal.SIGBREAK)
    for sig in candidates:
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass


def main():
    _configure_logging()
    _install_process_cleanup(get_profile_path())
    logger.info("Starting MCP server on stdio transport")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
