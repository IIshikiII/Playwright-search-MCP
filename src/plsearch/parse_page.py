from bs4 import BeautifulSoup


def parse_page(html: str) -> list[dict]:
    """Extract urls and page descriprions in json format

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
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for link in soup.find_all("a", href=True):
        h3 = link.find("h3")
        if h3:
            title = h3.get_text(strip=True)
            url = link["href"]
            
            snippet_div = link.find_next("div", class_="VwiC3b")
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


    return results