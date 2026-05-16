"""Unit tests for plsearch.main: _search_once, run orchestration, Web_search."""

from types import SimpleNamespace
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


def _make_browser_mock(page_contents: list[str]) -> tuple[MagicMock, MagicMock]:
    """Build a BrowserContext mock whose page.content() yields the given sequence.

    Returns (browser_mock, page_mock) so tests can assert on page-level cleanup.
    """
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.content = AsyncMock(side_effect=page_contents)
    page.close = AsyncMock()
    page.url = "https://www.google.com/search?q=mocked"

    browser = MagicMock()
    browser.new_page = AsyncMock(return_value=page)
    return browser, page


class _FakeAppContext:
    """Test double mimicking AppContext for `run()` orchestration tests.

    Tracks call counts so we can assert reveal/hide are invoked exactly when
    expected. If `headed_browser` is None and `reveal_for_captcha` is called,
    the test will fail loudly.
    """

    def __init__(
        self,
        headless_browser: MagicMock,
        headed_browser: MagicMock | None = None,
    ) -> None:
        self._headless = headless_browser
        self._headed = headed_browser
        self.get_browser_calls = 0
        self.reveal_calls = 0
        self.hide_calls = 0

    async def get_browser(self) -> MagicMock:
        self.get_browser_calls += 1
        return self._headless

    async def reveal_for_captcha(self) -> MagicMock:
        self.reveal_calls += 1
        if self._headed is None:
            raise AssertionError("reveal_for_captcha was called but no headed browser configured")
        return self._headed

    async def hide_after_captcha(self) -> None:
        self.hide_calls += 1


def _ctx_with(app: _FakeAppContext) -> MagicMock:
    """Build a FastMCP Context whose lifespan_context is the given app."""
    ctx = MagicMock()
    ctx.request_context = SimpleNamespace(lifespan_context=app)
    return ctx


class TestSearchOnce:
    async def test_no_captcha_returns_results(self) -> None:
        browser, page = _make_browser_mock([CLEAN_HTML, CLEAN_HTML])

        results = await main._search_once(browser, "q", wait_for_captcha=False)

        assert len(results) == 2
        assert results[0]["title"] == "Result One"
        page.close.assert_awaited_once()

    async def test_captcha_without_wait_returns_none(self) -> None:
        browser, page = _make_browser_mock([CAPTCHA_HTML])

        result = await main._search_once(browser, "q", wait_for_captcha=False)

        assert result is None
        page.close.assert_awaited_once()

    async def test_captcha_with_wait_solved_returns_results(self) -> None:
        # First content() sees CAPTCHA, wait_until_captcha_solved patched to True,
        # second content() (after wait_for_load_state) sees clean page.
        browser, page = _make_browser_mock([CAPTCHA_HTML, CLEAN_HTML])

        with patch.object(main, "wait_until_captcha_solved", new=AsyncMock(return_value=True)) as wait_mock:
            results = await main._search_once(browser, "q", wait_for_captcha=True)

        wait_mock.assert_awaited_once()
        assert len(results) == 2
        page.close.assert_awaited_once()

    async def test_captcha_with_wait_timeout_raises(self) -> None:
        browser, page = _make_browser_mock([CAPTCHA_HTML])

        with patch.object(main, "wait_until_captcha_solved", new=AsyncMock(return_value=False)):
            with pytest.raises(RuntimeError, match="CAPTCHA was not solved"):
                await main._search_once(browser, "q", wait_for_captcha=True)

        page.close.assert_awaited_once()

    async def test_goto_failure_closes_page(self) -> None:
        browser, page = _make_browser_mock([CLEAN_HTML, CLEAN_HTML])
        page.goto = AsyncMock(side_effect=RuntimeError("network down"))

        with pytest.raises(RuntimeError, match="network down"):
            await main._search_once(browser, "q", wait_for_captcha=False)

        page.close.assert_awaited_once()


class TestRun:
    async def test_no_captcha_does_not_reveal_or_hide(self) -> None:
        """Headless succeeds → no browser swap, just the cached headless instance."""
        headless, _ = _make_browser_mock([CLEAN_HTML, CLEAN_HTML])
        app = _FakeAppContext(headless_browser=headless)

        results = await main.run(app, "q")

        assert len(results) == 2
        assert app.get_browser_calls == 1
        assert app.reveal_calls == 0
        assert app.hide_calls == 0

    async def test_captcha_in_headless_triggers_reveal_then_hide(self) -> None:
        """Headless sees CAPTCHA → reveal headed → user solves → results parsed → hide."""
        headless, _ = _make_browser_mock([CAPTCHA_HTML])
        # Headed: first content() is still CAPTCHA (page just navigated), wait runs,
        # then post-wait content() is the clean page.
        headed, _ = _make_browser_mock([CAPTCHA_HTML, CLEAN_HTML])
        app = _FakeAppContext(headless_browser=headless, headed_browser=headed)

        with patch.object(main, "wait_until_captcha_solved", new=AsyncMock(return_value=True)):
            results = await main.run(app, "q")

        assert len(results) == 2
        assert app.reveal_calls == 1
        assert app.hide_calls == 1

    async def test_captcha_timeout_in_headed_still_hides(self) -> None:
        """Even if headed CAPTCHA times out, hide_after_captcha runs (finally clause)."""
        headless, _ = _make_browser_mock([CAPTCHA_HTML])
        headed, _ = _make_browser_mock([CAPTCHA_HTML])
        app = _FakeAppContext(headless_browser=headless, headed_browser=headed)

        with patch.object(main, "wait_until_captcha_solved", new=AsyncMock(return_value=False)):
            with pytest.raises(RuntimeError, match="CAPTCHA was not solved"):
                await main.run(app, "q")

        assert app.reveal_calls == 1
        assert app.hide_calls == 1


class TestAppContext:
    async def test_get_browser_caches_and_launches_headless(self) -> None:
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context = AsyncMock(side_effect=[MagicMock(), MagicMock()])
        app = main.AppContext(playwright=playwright, user_data_dir="/fake")

        first = await app.get_browser()
        second = await app.get_browser()

        assert first is second
        playwright.chromium.launch_persistent_context.assert_awaited_once()
        kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
        assert kwargs["headless"] is True

    async def test_reveal_closes_then_relaunches_headed(self) -> None:
        playwright = MagicMock()
        first_browser = MagicMock()
        first_browser.close = AsyncMock()
        second_browser = MagicMock()
        second_browser.close = AsyncMock()
        playwright.chromium.launch_persistent_context = AsyncMock(side_effect=[first_browser, second_browser])
        app = main.AppContext(playwright=playwright, user_data_dir="/fake")

        await app.get_browser()  # launches headless
        revealed = await app.reveal_for_captcha()

        assert revealed is second_browser
        first_browser.close.assert_awaited_once()
        kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
        assert kwargs["headless"] is False

    async def test_hide_closes_headed_relaunches_headless(self) -> None:
        playwright = MagicMock()
        b1, b2, b3 = MagicMock(), MagicMock(), MagicMock()
        b1.close = AsyncMock()
        b2.close = AsyncMock()
        playwright.chromium.launch_persistent_context = AsyncMock(side_effect=[b1, b2, b3])
        app = main.AppContext(playwright=playwright, user_data_dir="/fake")

        await app.get_browser()
        await app.reveal_for_captcha()
        await app.hide_after_captcha()

        b2.close.assert_awaited_once()
        # Third launch is the post-CAPTCHA headless one.
        assert playwright.chromium.launch_persistent_context.await_args.kwargs["headless"] is True
        # AppContext now holds the third browser.
        assert app._browser is b3

    async def test_shutdown_closes_and_clears(self) -> None:
        playwright = MagicMock()
        browser = MagicMock()
        browser.close = AsyncMock()
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=browser)
        app = main.AppContext(playwright=playwright, user_data_dir="/fake")

        await app.get_browser()
        await app.shutdown()

        browser.close.assert_awaited_once()
        assert app._browser is None


class TestWebSearch:
    async def test_happy_path_returns_parsed_results(self) -> None:
        headless, _ = _make_browser_mock([CLEAN_HTML, CLEAN_HTML])
        app = _FakeAppContext(headless_browser=headless)
        ctx = _ctx_with(app)

        results = await main.Web_search("query", ctx)

        assert len(results) == 2
        assert results[0]["type"] == "web_search_result"
        assert {"type", "title", "url", "page_content", "page_age"} <= results[0].keys()

    async def test_propagates_exception_after_logging(self, caplog: pytest.LogCaptureFixture) -> None:
        boom = RuntimeError("browser is gone")
        headless = MagicMock()
        headless.new_page = AsyncMock(side_effect=boom)
        app = _FakeAppContext(headless_browser=headless)
        ctx = _ctx_with(app)

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="browser is gone"):
                await main.Web_search("query", ctx)

        error_records = [
            r for r in caplog.records
            if r.levelname == "ERROR" and r.message == "Error during search request"
        ]
        assert error_records, "expected the error log line"
        assert error_records[0].exc_info is not None
        assert "browser is gone" in str(error_records[0].exc_info[1])
