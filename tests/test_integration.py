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


@pytest.mark.slow
def test_google_search_flow(page: Page) -> None:
    """Test complete Google search flow with real browser.

    This test demonstrates human-in-the-middle pattern for CAPTCHA handling.
    If CAPTCHA is detected, the test pauses and waits for manual resolution.

    Note: Run with --headed flag to see the browser window.
    """
    search_query = "python playwright tutorial"
    page.goto(f"{GOOGLE_SEARCH_URL}{search_query}")

    # Wait for page to load
    page.wait_for_load_state("domcontentloaded")

    # Check for CAPTCHA
    page_content = page.content()
    if is_captcha_page(page_content):
        # Wait for human to resolve CAPTCHA (automatic polling)
        resolved = wait_for_human_resolution(page, timeout=120)
        if not resolved:
            pytest.skip("Test skipped: CAPTCHA not solved within timeout")

        # Wait for page to reload after CAPTCHA solve
        page.wait_for_load_state("domcontentloaded")

        # Get fresh page content
        page_content = page.content()

    # Parse results
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
    """Test Google search with special characters in query."""
    search_query = "test+query&special=chars"
    page.goto(f"{GOOGLE_SEARCH_URL}{search_query}")

    page.wait_for_load_state("domcontentloaded")

    # Just verify page loaded without error
    assert "google.com" in page.url.lower() or "google" in page.title().lower()


@pytest.mark.slow
def test_empty_search_query(page: Page) -> None:
    """Test handling of empty search query."""
    page.goto(GOOGLE_SEARCH_URL)

    page.wait_for_load_state("domcontentloaded")

    # Google should redirect to home page or show search results
    assert "google" in page.url.lower() or "google" in page.title().lower()
