import logging

from bs4 import BeautifulSoup

from plsearch.config import SNIPPET_CLASS

logger = logging.getLogger("parse_page")


def parse_page(html: str) -> list[dict]:
    """Extract urls and page descriptions in json format

    Args:
        html (str): page content

    Returns:
        list[dict]: A list of dictionaries, each representing a search result
              with the following keys:
              - ``type`` (str): Always ``"web_search_result"``.
              - ``title`` (str): The title of the result (text from the ``<h3>`` tag).
              - ``url`` (str): The hyperlink associated with the result.
              - ``page_content`` (str): The snippet or description text (if
                available) extracted from the nearest ``<div class="VwiC3b">``.
              - ``page_age`` (str): An optional string indicating the age of the
                page (currently left empty).
    """
    logger.debug("Starting HTML parsing...")
    soup = BeautifulSoup(html, "html.parser")

    links = list(soup.find_all("a", href=True))
    logger.debug(f"Found {len(links)} links on page")

    results = []
    parsed_count = 0
    for link in links:
        h3 = link.find("h3")
        if h3:
            title = h3.get_text(strip=True)
            url = link["href"]

            snippet_div = link.find_next("div", class_=SNIPPET_CLASS)
            snippet = snippet_div.get_text(strip=True) if snippet_div else ""

            title = title.replace("\xa0", " ")
            snippet = snippet.replace("\xa0", " ")

            results.append({
                "type": "web_search_result",
                "title": title,
                "url": url,
                "page_content": snippet,
                "page_age": ""
            })
            parsed_count += 1

    logger.info(f"Parsing completed: extracted {parsed_count} results from {len(links)} links")
    return results
