from playwright.async_api import async_playwright, Playwright
from time import sleep
from urllib.parse import quote
import os
import logging
from dotenv import load_dotenv

load_dotenv()

from plsearch.parse_page import parse_page
import json
import sys

from typing import Any

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
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

mcp = FastMCP("Web search")

profile_path = os.getenv("PROFILE_DIR")


async def run(playwright: Playwright, quety: str):
    user_data_dir = profile_path
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
    query = quety
    logger.info(f"Executing search query: \"{query}\"")
    await page.goto(f"https://www.google.com/search?q={quote(query)}")
    logger.info(f"Page loaded: {page.url}")

    # other actions...
    # with open("test.html", "w", encoding="utf8") as f:
    #     f.write(page.content())

    logger.info("Parsing page content...")
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

        logger.info("Formatting JSON response...")
        result_json = json.dumps(search_results, indent=4, ensure_ascii=False)
        logger.info("Search request completed successfully")
        logger.info("=" * 60)

        print(result_json)
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
