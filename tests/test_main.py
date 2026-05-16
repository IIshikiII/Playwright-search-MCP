"""Unit tests for plsearch.main: drives `run` and `Web_search` with mocked Playwright."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plsearch import main
from plsearch.config import CAPTCHA_FORM_ID


CLEAN_HTML = """
<html><body>
  <a href="https://example.com/1"><h3>Result One</h3></a>
  <div class="VwiC3b">First snippet</div>
  <a href="https://example.com/2"><h3>Result Two</h3></a>
  <div class="VwiC3b">Second snippet</div>
</body></html>
"""

CAPTCHA_HTML = f'<html><body><form id="{CAPTCHA_FORM_ID}"></form></body></html>'


def _make_playwright_mock(page_contents: list[str]) -> MagicMock:
    """Build a Playwright mock whose page.content() yields the given sequence.

    Calls are: launch_persistent_context -> new_page -> (goto, wait_for_load_state,
    content...) -> close.
    """
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.content = AsyncMock(side_effect=page_contents)
    page.url = "https://www.google.com/search?q=mocked"

    browser = MagicMock()
    browser.new_page = AsyncMock(return_value=page)
    browser.close = AsyncMock()

    chromium = MagicMock()
    chromium.launch_persistent_context = AsyncMock(return_value=browser)

    playwright = MagicMock()
    playwright.chromium = chromium
    return playwright


class TestRun:
    @pytest.fixture(autouse=True)
    def _profile_dir_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # main.run calls get_profile_path(), which now raises on empty.
        # Keep these unit tests independent of the user's local .env.
        monkeypatch.setenv("PROFILE_DIR", "/fake/profile/path")

    async def test_no_captcha_parses_immediately(self) -> None:
        # First content() = the pre-CAPTCHA check (clean), second = the final parse.
        playwright = _make_playwright_mock([CLEAN_HTML, CLEAN_HTML])

        results = await main.run(playwright, "anything")

        assert len(results) == 2
        assert results[0]["title"] == "Result One"
        assert results[0]["url"] == "https://example.com/1"
        assert results[1]["title"] == "Result Two"

    async def test_captcha_then_resolved_parses_clean_page(self) -> None:
        # content() sequence: initial check sees CAPTCHA, then final parse sees clean.
        # wait_until_captcha_solved is patched to True so we don't poll the mock.
        playwright = _make_playwright_mock([CAPTCHA_HTML, CLEAN_HTML])

        with patch.object(main, "wait_until_captcha_solved", new=AsyncMock(return_value=True)) as wait_mock:
            results = await main.run(playwright, "anything")

        wait_mock.assert_awaited_once()
        assert len(results) == 2

    async def test_captcha_timeout_raises_runtime_error(self) -> None:
        playwright = _make_playwright_mock([CAPTCHA_HTML])

        with patch.object(main, "wait_until_captcha_solved", new=AsyncMock(return_value=False)):
            with pytest.raises(RuntimeError, match="CAPTCHA was not solved"):
                await main.run(playwright, "anything")

    async def test_goto_failure_propagates(self) -> None:
        playwright = _make_playwright_mock([CLEAN_HTML, CLEAN_HTML])
        # Override goto on the page we just built.
        page = await playwright.chromium.launch_persistent_context.return_value.new_page()
        page.goto = AsyncMock(side_effect=RuntimeError("network down"))

        with pytest.raises(RuntimeError, match="network down"):
            await main.run(playwright, "anything")


class TestWebSearch:
    async def test_propagates_exception_after_logging(self, caplog: pytest.LogCaptureFixture) -> None:
        """Web_search must log the exception and re-raise it."""
        boom = RuntimeError("playwright unavailable")

        # async_playwright() is called as a context manager — patch it to raise on enter.
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=boom)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch.object(main, "async_playwright", return_value=cm):
            with caplog.at_level("ERROR"):
                with pytest.raises(RuntimeError, match="playwright unavailable"):
                    await main.Web_search("query")

        error_records = [
            r for r in caplog.records
            if r.levelname == "ERROR" and r.message == "Error during search request"
        ]
        assert error_records, "expected the error log line"
        # logger.exception() attaches the live exc_info; the message itself is fixed
        # (L4: don't duplicate str(exc) in the format string).
        assert error_records[0].exc_info is not None
        assert "playwright unavailable" in str(error_records[0].exc_info[1])
