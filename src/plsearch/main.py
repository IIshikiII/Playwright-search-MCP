from playwright.async_api import async_playwright, Playwright
from time import sleep
from urllib.parse import quote
import os
import logging
from dotenv import load_dotenv

load_dotenv()

from plsearch.parse_page import parse_page
import json

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# Настройка красивого логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger("web_search")

mcp = FastMCP("Web search")

profile_path = os.getenv("PROFILE_DIR")


def run(playwright: Playwright):
    user_data_dir = profile_path
    logger.info(f"Путь к данным профиля: {user_data_dir}")

    chromium = playwright.chromium
    logger.info("Запуск браузера Chrome в видимом режиме...")
    browser = chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel="chrome",
        headless=False
    )
    logger.success("Браузер успешно запущен")

    page = browser.new_page()
    query = "как научиться программировать"
    logger.info(f"Выполнение поискового запроса: \"{query}\"")
    page.goto(f"https://www.google.com/search?q={quote(query)}")
    logger.success(f"Страница загружена: {page.url}")

    # other actions...
    # with open("test.html", "w", encoding="utf8") as f:
    #     f.write(page.content())

    logger.info("Парсинг содержимого страницы...")
    results = parse_page(page.content())
    logger.success(f"Извлечено {len(results)} результатов поиска")

    browser.close()
    logger.info("Браузер закрыт")

    return results

@mcp.tool()
async def Web_search(state: str) -> str:
    """Make google search query

    Args:
        state: Clear google search query
    """
    logger.info("=" * 60)
    logger.info("Начало выполнения поискового запроса через MCP")
    logger.info(f"Входные данные: {state}")
    logger.info("-" * 60)

    with async_playwright() as playwright:
        search_results = run(playwright)

    logger.info("Формирование JSON-ответа...")
    result_json = json.dumps(search_results, indent=4, ensure_ascii=False)
    logger.success("Поисковый запрос успешно завершен")
    logger.info("=" * 60)

    print(result_json)
    return result_json


def main():
    # Initialize and run the server
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()