from playwright.async_api import async_playwright, Playwright
from urllib.parse import quote
import logging
import logging.handlers
import os
import sys
from dotenv import load_dotenv

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

from mcp.server.fastmcp import FastMCP


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

mcp = FastMCP("Web_search")

async def run(playwright: Playwright, query: str):
    user_data_dir = get_profile_path()
    logger.info("Profile data path: %s", user_data_dir)

    chromium = playwright.chromium
    logger.info("Starting Chrome browser in visible mode...")
    browser = await chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel="chrome",
        headless=False
    )
    logger.info("Browser successfully started")

    page = await browser.new_page()
    logger.info("Executing search query: %r", query)
    await page.goto(f"{GOOGLE_SEARCH_URL}{quote(query)}")
    logger.info("Page loaded: %s", page.url)

    # Check for CAPTCHA page and wait for resolution if needed
    page_content = await page.content()
    if is_captcha_page(page_content):
        logger.warning("CAPTCHA detected! Solve it manually in the browser...")
        solved = await wait_until_captcha_solved(page, timeout=CAPTCHA_WAIT_TIMEOUT_SECONDS)
        if not solved:
            raise RuntimeError(
                f"CAPTCHA was not solved within {CAPTCHA_WAIT_TIMEOUT_SECONDS}s"
            )
        logger.info("CAPTCHA solved! Continuing...")

    await page.wait_for_load_state("domcontentloaded")
    page_content = await page.content()
    results = parse_page(page_content)
    logger.info("Extracted %d search results", len(results))

    await browser.close()
    logger.info("Browser closed")

    return results


@mcp.tool()
async def Web_search(state: str) -> list[dict]:
    """Make google search query

    Args:
        state: Clear google search query
    """
    logger.info("Starting MCP search request: %s", state)

    try:
        async with async_playwright() as playwright:
            search_results = await run(playwright, state)

        logger.info("Search request completed successfully")
        return search_results
    except Exception:
        logger.exception("Error during search request")
        raise


def main():
    _configure_logging()
    logger.info("Starting MCP server on stdio transport")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
