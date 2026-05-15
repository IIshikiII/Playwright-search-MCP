from playwright.async_api import async_playwright, Playwright
from urllib.parse import quote
import os
import logging
from dotenv import load_dotenv
import asyncio

load_dotenv()

from plsearch.parse_page import parse_page
from plsearch.config import GOOGLE_SEARCH_URL, get_profile_path, is_captcha_page
import sys

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from mcp.server.fastmcp import FastMCP

# Настройка логирования
log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(log_dir, exist_ok=True)

log_file = os.path.join(log_dir, "search.log")

# Create custom handler with line buffering
class LineBufferedStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes on every record"""
    def emit(self, record):
        super().emit(record)
        self.flush()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        LineBufferedStreamHandler()
    ]
)

logger = logging.getLogger("web_search")

mcp = FastMCP("Web_search")

async def run(playwright: Playwright, query: str):
    user_data_dir = get_profile_path()
    logger.info(f"Profile data path: {user_data_dir}")

    chromium = playwright.chromium
    logger.info("Starting Chrome browser in visible mode...")
    browser = await chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel="chrome",
        headless=False
    )
    logger.info("Browser successfully started")

    page = await browser.new_page()
    logger.info(f"Executing search query: \"{query}\"")
    await page.goto(f"{GOOGLE_SEARCH_URL}{quote(query)}")
    logger.info(f"Page loaded: {page.url}")

    # Check for CAPTCHA page and wait for resolution if needed
    page_content = await page.content()
    if is_captcha_page(page_content):
        logger.warning("CAPTCHA detected! Solve it manually in the browser...")

        # Wait for CAPTCHA to be solved by monitoring page state
        while True:
            try:
                # Wait for any navigation to complete
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass  # Continue if no navigation is happening

            try:
                new_content = await page.content()
                # Check if CAPTCHA is gone
                if not is_captcha_page(new_content):
                    logger.info("CAPTCHA solved! Continuing...")
                    break
            except Exception as e:
                logger.debug(f"Page content not available yet: {e}")
                await asyncio.sleep(1)

        # Wait for page to load after CAPTCHA solve
        await page.wait_for_load_state("domcontentloaded")
    else:
        # No CAPTCHA - wait for initial page load
        await page.wait_for_load_state("domcontentloaded")

    # Single parsing point - parse the current page content (either after initial load or after CAPTCHA solve)
    page_content = await page.content()
    results = parse_page(page_content)
    logger.info(f"Extracted {len(results)} search results")

    await browser.close()
    logger.info("Browser closed")

    return results


@mcp.tool()
async def Web_search(state: str) -> list[dict]:
    """Make google search query

    Args:
        state: Clear google search query
    """
    logger.info("=" * 60)
    logger.info("Starting MCP search request")
    logger.info(f"Input data: {state}")
    logger.info("-" * 60)

    try:
        async with async_playwright() as playwright:
            search_results = await run(playwright, state)

        logger.info("Search request completed successfully")
        logger.info("=" * 60)

        return search_results
    except Exception as e:
        logger.exception(f"Error during search request: {e}")
        raise


def main():
    logger.info("=" * 60)
    logger.info("Starting MCP server on stdio transport")
    logger.info("=" * 60)
    # Initialize and run the server
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
