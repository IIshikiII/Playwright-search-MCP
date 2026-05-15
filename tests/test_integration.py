"""Integration tests for web search functionality.

These tests use real browser automation to verify the complete search flow.
For tests requiring human interaction (e.g., CAPTCHA), the test will pause and
wait for manual resolution before continuing.

Run with: pytest tests/test_integration.py --slow
"""

import pytest
from playwright.sync_api import Page

from plsearch.config import GOOGLE_SEARCH_URL, is_captcha_page
from plsearch.parse_page import parse_page


def wait_for_human_resolution(page: Page, timeout: int = 120) -> bool:
    """Wait for human to manually resolve a challenge (e.g., CAPTCHA).

    The function polls the page periodically to detect when CAPTCHA is solved.

    Args:
        page: Playwright page object to monitor.
        timeout: Maximum time to wait in seconds.

    Returns:
        True if CAPTCHA was solved within timeout, False otherwise.
    """
    import time

    print("\n" + "=" * 60)
    print("⚠️  HUMAN IN THE MIDDLE - CHALLENGE DETECTED")
    print("=" * 60)
    print("Please manually resolve the challenge in the browser window.")
    print("The test will automatically continue once CAPTCHA is solved.")
    print(f"(Timeout: {timeout} seconds)")
    print("=" * 60 + "\n")

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            current_content = page.content()
            # Check if CAPTCHA is gone
            if not is_captcha_page(current_content):
                print("CAPTCHA solved! Continuing test...")
                return True
        except Exception as e:
            pass  # Page might be navigating, continue polling

        time.sleep(1)  # Poll every second

    print(f"\nTimeout waiting for CAPTCHA resolution after {timeout} seconds.")
    return False


def _settle_after_possible_captcha(page: Page) -> str:
    """Return final page content, handling CAPTCHA if Google challenges us.

    Skips the calling test if CAPTCHA appears and isn't solved within the
    polling timeout. The returned string is guaranteed to be from a
    non-CAPTCHA page so callers can run shape/results assertions on it.
    """
    page.wait_for_load_state("domcontentloaded")
    page_content = page.content()
    if is_captcha_page(page_content):
        if not wait_for_human_resolution(page, timeout=120):
            pytest.skip("CAPTCHA not solved within timeout")
        page.wait_for_load_state("domcontentloaded")
        page_content = page.content()
    return page_content


@pytest.mark.slow
def test_google_search_flow(page: Page) -> None:
    """Test complete Google search flow with real browser.

    This test demonstrates human-in-the-middle pattern for CAPTCHA handling.
    If CAPTCHA is detected, the test pauses and waits for manual resolution.

    Note: Run with --headed flag to see the browser window.
    """
    search_query = "python playwright tutorial"
    page.goto(f"{GOOGLE_SEARCH_URL}{search_query}")

    page_content = _settle_after_possible_captcha(page)
    results = parse_page(page_content)

    # Verify we got some results
    assert len(results) > 0, "Expected at least one search result"

    # Verify result structure
    for result in results:
        assert "type" in result
        assert result["type"] == "web_search_result"
        assert "title" in result
        assert "url" in result
        assert "page_content" in result
        assert "page_age" in result


@pytest.mark.slow
def test_google_search_with_special_characters(page: Page) -> None:
    """Special-character queries still yield parseable Google results."""
    search_query = "test+query&special=chars"
    page.goto(f"{GOOGLE_SEARCH_URL}{search_query}")

    page_content = _settle_after_possible_captcha(page)
    results = parse_page(page_content)

    assert "google" in page.url.lower()
    assert len(results) > 0, "Expected at least one result for a special-char query"


@pytest.mark.slow
def test_empty_search_query(page: Page) -> None:
    """An empty `q=` lands on a real Google page (not the CAPTCHA wall)."""
    page.goto(GOOGLE_SEARCH_URL)

    page_content = _settle_after_possible_captcha(page)

    assert "google" in page.url.lower()
    assert not is_captcha_page(page_content), "Landed on the CAPTCHA wall, not Google"
