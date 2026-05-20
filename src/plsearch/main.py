from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import quote
import asyncio
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
    get_http_host,
    get_http_port,
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


def _configure_logging() -> None:
    """Configure root logging for the MCP server process.

    Called from `main()` so that importing `plsearch.main` (e.g. from tests)
    does not reconfigure the root logger as a side effect. Logs go to both
    the rotating file and stderr; under HTTP transport stdout is not a JSON-RPC
    channel, so stderr is safe (unlike the prior stdio mode which had to
    suppress it to dodge MCP Inspector bug #654).

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

    Under HTTP transport multiple clients can hit the server in parallel. The
    ``request_lock`` serializes ``run()`` calls so concurrent searches don't
    share a page handle or have the browser yanked from under them by a
    CAPTCHA reveal in another request. Single-client throughput is unchanged.
    """

    playwright: Playwright
    user_data_dir: str
    _browser: BrowserContext | None = None
    request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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


# Process-singleton AppContext shared across all MCP sessions. FastMCP under
# streamable-http opens a new MCP session per HTTP client; if each session
# created its own Playwright + Chrome, they'd fight for the single
# `user_data_dir` OS lock and only the first session would actually launch.
# The singleton means: one Playwright, one Chrome, one profile — many MCP
# sessions sharing the AppContext.request_lock for serialization.
_playwright_singleton: Playwright | None = None
_app_singleton: AppContext | None = None
_singleton_init_lock: asyncio.Lock | None = None


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Per-session lifespan that returns the PROCESS-wide AppContext.

    First MCP session in the process creates the singleton (Playwright start +
    AppContext); subsequent sessions reuse it. No teardown on session exit —
    that would close the shared Chrome under any other live sessions. Process
    cleanup runs via atexit and SessionRegistry's next-start sweep.

    Profile-mutex (kill prior server, clear Singleton locks) ran already in
    ``main()`` before uvicorn bound the port — by the time lifespan fires for
    the first session, that's done.
    """
    global _playwright_singleton, _app_singleton, _singleton_init_lock
    # asyncio.Lock must be created inside a running loop; defer to first call.
    if _singleton_init_lock is None:
        _singleton_init_lock = asyncio.Lock()
    async with _singleton_init_lock:
        if _app_singleton is None:
            user_data_dir = get_profile_path()
            logger.info(
                "First MCP session — starting shared Playwright (profile=%s)",
                user_data_dir,
            )
            _playwright_singleton = await async_playwright().start()
            _app_singleton = AppContext(
                playwright=_playwright_singleton, user_data_dir=user_data_dir
            )
    try:
        yield _app_singleton
    finally:
        # Intentionally no shutdown here. Other live MCP sessions still hold
        # references to the singleton; tearing down Chrome would crash their
        # in-flight requests. Process-exit cleanup is the right scope.
        pass


async def _shutdown_singletons() -> None:
    """Async teardown of the process singleton. Called from sync atexit via asyncio.run."""
    global _playwright_singleton, _app_singleton
    if _app_singleton is not None:
        try:
            await _app_singleton.shutdown()
        except Exception:
            logger.exception("AppContext shutdown failed")
        _app_singleton = None
    if _playwright_singleton is not None:
        try:
            await _playwright_singleton.stop()
        except Exception:
            logger.exception("Playwright stop failed")
        _playwright_singleton = None


mcp = FastMCP(
    "Web_search",
    lifespan=lifespan,
    host=get_http_host(),
    port=get_http_port(),
)


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
    seen: set[str] = set()
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
            # Google reranks between &start=N offsets and can list the same URL
            # multiple times within a page; dedup by URL so `collected` and the
            # `len(collected) >= limit` guard count only unique hits.
            for r in page_results:
                if r["url"] not in seen:
                    seen.add(r["url"])
                    collected.append(r)

        results = collected[:limit]
        logger.info("Collected %d results (asked %d)", len(results), limit)
        return results
    finally:
        await page.close()


async def run(app: AppContext, query: str, limit: int) -> list[dict]:
    """Headless-first multi-page search; falls back to visible Chrome on CAPTCHA.

    Serialized via ``app.request_lock`` so concurrent HTTP clients can't have
    their ``page`` handle invalidated by a CAPTCHA-reveal browser swap
    happening in another request.
    """
    async with app.request_lock:
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
        # Close the shared Chrome / Playwright if we ever spun them up.
        # asyncio.run is safe here: uvicorn's loop is already stopped by the
        # time atexit fires, so this gets a fresh loop.
        if _app_singleton is not None or _playwright_singleton is not None:
            try:
                asyncio.run(_shutdown_singletons())
            except Exception:
                logger.exception("atexit singleton shutdown failed")
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
    user_data_dir = get_profile_path()

    # Profile-mutex BEFORE uvicorn binds the port. lifespan() runs lazily
    # under streamable-http (on first MCP session), so we can't put this
    # there — by the time lifespan fires, port-bind has already crashed
    # against any zombie server still owning :8765.
    SessionRegistry(user_data_dir).claim()
    cleanup_stale_profile_locks(user_data_dir)

    _install_process_cleanup(user_data_dir)
    host, port = get_http_host(), get_http_port()
    logger.info(
        "Starting MCP server on streamable-http transport at http://%s:%d/mcp",
        host, port,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
