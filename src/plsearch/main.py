from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote
import logging
import logging.handlers
import os
import sys

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Playwright, async_playwright

load_dotenv()

from plsearch.parse_page import parse_page
from plsearch.config import (
    GOOGLE_SEARCH_URL,
    LOG_DIR,
    LOG_FILE,
    get_profile_path,
    is_captcha_page,
    wait_until_captcha_solved,
)

CAPTCHA_WAIT_TIMEOUT_SECONDS = 120.0

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
    does not reconfigure the root logger as a side effect.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.handlers.RotatingFileHandler(
                LOG_FILE,
                maxBytes=5_000_000,
                backupCount=3,
                encoding="utf-8",
            ),
            LineBufferedStreamHandler(),
        ],
    )


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
    """Start Playwright + headless persistent Chrome at server boot; tear down on exit.

    The default mode is headless: no visible window during normal operation. The
    visible browser only opens when `_search_once` detects CAPTCHA — see `run()`.
    """
    user_data_dir = get_profile_path()
    logger.info("Lifespan: starting Playwright (profile=%s)", user_data_dir)
    playwright = await async_playwright().start()
    app = AppContext(playwright=playwright, user_data_dir=user_data_dir)
    try:
        await app.get_browser()  # eager so the first call is fast
        logger.info("Lifespan: headless browser ready")
        yield app
    finally:
        logger.info("Lifespan: shutting down")
        try:
            await app.shutdown()
        finally:
            await playwright.stop()


mcp = FastMCP("Web_search", lifespan=lifespan)


async def _search_once(
    browser: BrowserContext,
    query: str,
    *,
    wait_for_captcha: bool,
) -> list[dict] | None:
    """Run one search attempt on the given browser.

    Returns the parsed results on success. Returns ``None`` if CAPTCHA is detected
    and ``wait_for_captcha`` is False — the caller is expected to swap to a visible
    browser and retry with ``wait_for_captcha=True``.
    """
    page = await browser.new_page()
    try:
        url = f"{GOOGLE_SEARCH_URL}{quote(query)}"
        logger.info("Executing search query: %r", query)
        await page.goto(url)
        logger.info("Page loaded: %s", page.url)

        page_content = await page.content()
        if is_captcha_page(page_content):
            if not wait_for_captcha:
                logger.warning("CAPTCHA detected in headless mode — will retry in visible browser")
                return None
            logger.warning("CAPTCHA detected — waiting for user to solve in visible browser...")
            solved = await wait_until_captcha_solved(
                page, timeout=CAPTCHA_WAIT_TIMEOUT_SECONDS
            )
            if not solved:
                raise RuntimeError(
                    f"CAPTCHA was not solved within {CAPTCHA_WAIT_TIMEOUT_SECONDS}s"
                )
            logger.info("CAPTCHA solved! Continuing...")

        await page.wait_for_load_state("domcontentloaded")
        page_content = await page.content()
        results = parse_page(page_content)
        logger.info("Extracted %d search results", len(results))
        return results
    finally:
        await page.close()


async def run(app: AppContext, query: str) -> list[dict]:
    """Headless-first search; falls back to visible Chrome only if CAPTCHA appears."""
    browser = await app.get_browser()
    results = await _search_once(browser, query, wait_for_captcha=False)
    if results is not None:
        return results

    # Headless saw CAPTCHA. Swap to visible Chrome with the same profile, let the
    # user solve, then always swap back to headless for the next call.
    try:
        headed = await app.reveal_for_captcha()
        return await _search_once(headed, query, wait_for_captcha=True)
    finally:
        await app.hide_after_captcha()


@mcp.tool()
async def Web_search(state: str, ctx: Context) -> list[dict]:
    """Make google search query

    Args:
        state: Clear google search query
    """
    logger.info("Starting MCP search request: %s", state)
    try:
        app = ctx.request_context.lifespan_context
        results = await run(app, state)
        logger.info("Search request completed successfully")
        return results
    except Exception:
        logger.exception("Error during search request")
        raise


def main():
    _configure_logging()
    logger.info("Starting MCP server on stdio transport")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
