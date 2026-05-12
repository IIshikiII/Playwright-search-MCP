"""Unit tests for parse_page module."""

import pytest
from plsearch.parse_page import parse_page


def test_parse_page_with_valid_results() -> None:
    """Test parsing HTML with valid search results."""
    html = """
    <html>
        <body>
            <a href="https://example.com">
                <h3>Example Title</h3>
            </a>
            <div class="VwiC3b">This is a snippet description.</div>
        </body>
    </html>
    """
    results = parse_page(html)

    assert len(results) == 1
    assert results[0]["type"] == "web_search_result"
    assert results[0]["title"] == "Example Title"
    assert results[0]["url"] == "https://example.com"
    assert results[0]["page_content"] == "This is a snippet description."
    assert results[0]["page_age"] == ""


def test_parse_page_with_multiple_results() -> None:
    """Test parsing HTML with multiple search results."""
    html = """
    <html>
        <body>
            <a href="https://example1.com">
                <h3>Title 1</h3>
            </a>
            <div class="VwiC3b">Snippet 1</div>
            <a href="https://example2.com">
                <h3>Title 2</h3>
            </a>
            <div class="VwiC3b">Snippet 2</div>
        </body>
    </html>
    """
    results = parse_page(html)

    assert len(results) == 2
    assert results[0]["title"] == "Title 1"
    assert results[1]["title"] == "Title 2"


def test_parse_page_with_nbsp_characters() -> None:
    """Test parsing HTML with non-breaking space characters."""
    html = """
    <html>
        <body>
            <a href="https://example.com">
                <h3>Title&nbsp;with&nbsp;spaces</h3>
            </a>
            <div class="VwiC3b">Snippet&nbsp;text</div>
        </body>
    </html>
    """
    results = parse_page(html)

    assert results[0]["title"] == "Title with spaces"
    assert results[0]["page_content"] == "Snippet text"


def test_parse_page_without_snippet() -> None:
    """Test parsing HTML without snippet div."""
    html = """
    <html>
        <body>
            <a href="https://example.com">
                <h3>Title Only</h3>
            </a>
        </body>
    </html>
    """
    results = parse_page(html)

    assert len(results) == 1
    assert results[0]["page_content"] == ""


def test_parse_page_with_empty_html() -> None:
    """Test parsing empty HTML."""
    html = ""
    results = parse_page(html)

    assert len(results) == 0


def test_parse_page_without_h3() -> None:
    """Test parsing links without h3 tags."""
    html = """
    <html>
        <body>
            <a href="https://example.com">Link without title</a>
        </body>
    </html>
    """
    results = parse_page(html)

    assert len(results) == 0


def test_parse_page_preserves_url() -> None:
    """Test that URLs are preserved correctly."""
    html = """
    <html>
        <body>
            <a href="/url?q=https://test.com/path&amp;other=param">
                <h3>Test Title</h3>
            </a>
        </body>
    </html>
    """
    results = parse_page(html)

    # BeautifulSoup decodes HTML entities, so &amp; becomes &
    assert results[0]["url"] == "/url?q=https://test.com/path&other=param"


def test_parse_page_logs_debug_info(caplog: pytest.LogCaptureFixture) -> None:
    """Test that parse_page logs debug information."""
    html = """
    <html>
        <body>
            <a href="https://example.com">
                <h3>Test</h3>
            </a>
        </body>
    </html>
    """
    with caplog.at_level("DEBUG"):
        parse_page(html)

    assert "Starting HTML parsing" in caplog.text
    assert "Found 1 links on page" in caplog.text
    assert "Parsing completed" in caplog.text
