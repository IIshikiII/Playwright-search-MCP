"""Unit tests for plsearch.main: _search, run orchestration, Web_search."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plsearch import main
from plsearch.config import CAPTCHA_FORM_ID, MAX_PAGES, RESULTS_PER_PAGE


CLEAN_HTML = """
<html><body>
  <a href="https://example.com/1"><h3>Result One</h3></a>
  <div class="VwiC3b">First snippet</div>
  <a href="https://example.com/2"><h3>Result Two</h3></a>
  <div class="VwiC3b">Second snippet</div>
</body></html>
"""

PAGE2_HTML = """
<html><body>
  <a href="https://example.com/3"><h3>Result Three</h3></a>
  <div class="VwiC3b">Third snippet</div>
  <a href="https://example.com/4"><h3>Result Four</h3></a>
  <div class="VwiC3b">Fourth snippet</div>
</body></html>
"""

EMPTY_HTML = "<html><body></body></html>"
CAPTCHA_HTML = f'<html><body><form id="{CAPTCHA_FORM_ID}"></form></body></html>'


def _html_for_page(page_idx: int, *, count: int = 2) -> str:
    """Build a Google-shaped HTML with unique URLs per page index."""
    parts = ["<html><body>"]
    for r in range(count):
        url = f"https://example.com/p{page_idx}_r{r}"
        parts.append(f'<a href="{url}"><h3>p{page_idx}_r{r}</h3></a>')
        parts.append('<div class="VwiC3b">snippet</div>')
    parts.append("</body></html>")
    return "".join(parts)


def _make_browser_mock(page_contents: list[str]) -> tuple[MagicMock, MagicMock]:
    """Build a BrowserContext mock whose page.content() yields the given sequence.

    Returns (browser_mock, page_mock) so tests can assert on page-level cleanup
    and on the sequence of `goto` URLs.
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

    Carries a real ``asyncio.Lock`` so ``run()`` can serialize against it the
    same way it does against the production AppContext.
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
        self.request_lock = asyncio.Lock()

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


class TestSearch:
    async def test_single_page_satisfies_limit(self) -> None:
        # 2 content() calls per page (captcha check + post-load).
        browser, page = _make_browser_mock([CLEAN_HTML, CLEAN_HTML])

        results = await main._search(browser, "q", limit=2, wait_for_captcha=False)

        assert len(results) == 2
        assert results[0]["title"] == "Result One"
        page.close.assert_awaited_once()
        page.goto.assert_awaited_once()

    async def test_walks_multiple_pages_to_fill_limit(self) -> None:
        # Page 1: 2 results, Page 2: 2 different results → limit 4 needs both.
        browser, page = _make_browser_mock([CLEAN_HTML, CLEAN_HTML, PAGE2_HTML, PAGE2_HTML])

        results = await main._search(browser, "q", limit=4, wait_for_captcha=False)

        assert len(results) == 4
        assert page.goto.await_count == 2
        first_url = page.goto.await_args_list[0].args[0]
        second_url = page.goto.await_args_list[1].args[0]
        assert "start=0" in first_url
        assert f"start={RESULTS_PER_PAGE}" in second_url

    async def test_trims_to_limit_when_page_overshoots(self) -> None:
        browser, page = _make_browser_mock([CLEAN_HTML, CLEAN_HTML])

        results = await main._search(browser, "q", limit=1, wait_for_captcha=False)

        assert len(results) == 1
        page.goto.assert_awaited_once()

    async def test_stops_when_page_returns_no_results(self) -> None:
        # Page 1: 2 real results, Page 2: empty → walk stops early.
        browser, page = _make_browser_mock([CLEAN_HTML, CLEAN_HTML, EMPTY_HTML, EMPTY_HTML])

        results = await main._search(browser, "q", limit=20, wait_for_captcha=False)

        assert len(results) == 2
        assert page.goto.await_count == 2

    async def test_respects_max_pages_cap(self) -> None:
        # Each page contributes 2 unique results (across pages too — see
        # _html_for_page). Walker must stop at MAX_PAGES regardless of limit.
        page_htmls = [_html_for_page(p) for p in range(MAX_PAGES + 2)]
        # Two content() calls per page: captcha check + post-load.
        contents = [html for html in page_htmls for _ in range(2)]
        browser, page = _make_browser_mock(contents)

        results = await main._search(browser, "q", limit=1000, wait_for_captcha=False)

        assert len(results) == MAX_PAGES * 2
        assert page.goto.await_count == MAX_PAGES

    async def test_captcha_on_first_page_returns_none(self) -> None:
        browser, page = _make_browser_mock([CAPTCHA_HTML])

        result = await main._search(browser, "q", limit=10, wait_for_captcha=False)

        assert result is None
        page.close.assert_awaited_once()

    async def test_captcha_on_later_page_returns_partial(self) -> None:
        # Page 1: 2 results. Page 2: captcha → bail, return the partial 2.
        browser, page = _make_browser_mock([CLEAN_HTML, CLEAN_HTML, CAPTCHA_HTML])

        results = await main._search(browser, "q", limit=20, wait_for_captcha=False)

        assert len(results) == 2
        assert page.goto.await_count == 2
        page.close.assert_awaited_once()

    async def test_captcha_with_wait_solved_returns_results(self) -> None:
        # Captcha-check sees CAPTCHA, wait_until_captcha_solved patched to True,
        # post-load content() sees clean page.
        browser, page = _make_browser_mock([CAPTCHA_HTML, CLEAN_HTML])

        with patch.object(main, "wait_until_captcha_solved", new=AsyncMock(return_value=True)) as wait_mock:
            results = await main._search(browser, "q", limit=2, wait_for_captcha=True)

        wait_mock.assert_awaited_once()
        assert len(results) == 2
        page.close.assert_awaited_once()

    async def test_captcha_with_wait_timeout_raises(self) -> None:
        browser, page = _make_browser_mock([CAPTCHA_HTML])

        with patch.object(main, "wait_until_captcha_solved", new=AsyncMock(return_value=False)):
            with pytest.raises(RuntimeError, match="CAPTCHA was not solved"):
                await main._search(browser, "q", limit=10, wait_for_captcha=True)

        page.close.assert_awaited_once()

    async def test_dedups_urls_across_pages(self) -> None:
        """Google reranks between &start=N offsets; the same URL can appear on
        multiple pages. _search must dedup so the walker doesn't return phantom
        duplicates and doesn't stop early thinking the limit was reached."""
        page1 = """
        <html><body>
          <a href="https://example.com/a"><h3>Result A</h3></a>
          <div class="VwiC3b">snippet A</div>
          <a href="https://example.com/b"><h3>Result B</h3></a>
          <div class="VwiC3b">snippet B</div>
        </body></html>
        """
        # Page 2 repeats B (the dup) and introduces C.
        page2 = """
        <html><body>
          <a href="https://example.com/b"><h3>Result B again</h3></a>
          <div class="VwiC3b">snippet B</div>
          <a href="https://example.com/c"><h3>Result C</h3></a>
          <div class="VwiC3b">snippet C</div>
        </body></html>
        """
        browser, page = _make_browser_mock([page1, page1, page2, page2])

        results = await main._search(browser, "q", limit=3, wait_for_captcha=False)

        urls = [r["url"] for r in results]
        assert urls == [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ]
        assert len(set(urls)) == len(urls), f"duplicates leaked: {urls}"
        # Page 1 alone has only 2 unique hits; walker must fetch page 2 to fill limit=3.
        assert page.goto.await_count == 2

    async def test_dedups_urls_within_a_single_page(self) -> None:
        """Google sometimes emits two <a> tags (e.g., thumbnail + title) for
        the same result — same URL twice on one page. Dedup must catch that."""
        page1 = """
        <html><body>
          <a href="https://example.com/a"><h3>A first link</h3></a>
          <div class="VwiC3b">snippet A</div>
          <a href="https://example.com/a"><h3>A duplicate link</h3></a>
          <div class="VwiC3b">snippet A again</div>
          <a href="https://example.com/b"><h3>Result B</h3></a>
          <div class="VwiC3b">snippet B</div>
        </body></html>
        """
        browser, page = _make_browser_mock([page1, page1])

        # limit=2 stops the walker after page 1 (where both unique URLs live).
        results = await main._search(browser, "q", limit=2, wait_for_captcha=False)

        urls = [r["url"] for r in results]
        assert urls == ["https://example.com/a", "https://example.com/b"]
        page.goto.assert_awaited_once()

    async def test_goto_failure_closes_page(self) -> None:
        browser, page = _make_browser_mock([CLEAN_HTML])
        page.goto = AsyncMock(side_effect=RuntimeError("network down"))

        with pytest.raises(RuntimeError, match="network down"):
            await main._search(browser, "q", limit=10, wait_for_captcha=False)

        page.close.assert_awaited_once()


class TestRun:
    async def test_no_captcha_does_not_reveal_or_hide(self) -> None:
        """Headless succeeds → no browser swap, just the cached headless instance."""
        headless, _ = _make_browser_mock([CLEAN_HTML, CLEAN_HTML])
        app = _FakeAppContext(headless_browser=headless)

        results = await main.run(app, "q", limit=2)

        assert len(results) == 2
        assert app.get_browser_calls == 1
        assert app.reveal_calls == 0
        assert app.hide_calls == 0

    async def test_captcha_in_headless_triggers_reveal_then_hide(self) -> None:
        """Headless sees CAPTCHA on page 1 → reveal headed → user solves → hide."""
        headless, _ = _make_browser_mock([CAPTCHA_HTML])
        # Headed: captcha-check returns CAPTCHA, wait runs, post-load is clean.
        headed, _ = _make_browser_mock([CAPTCHA_HTML, CLEAN_HTML])
        app = _FakeAppContext(headless_browser=headless, headed_browser=headed)

        with patch.object(main, "wait_until_captcha_solved", new=AsyncMock(return_value=True)):
            results = await main.run(app, "q", limit=2)

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
                await main.run(app, "q", limit=10)

        assert app.reveal_calls == 1
        assert app.hide_calls == 1

    async def test_partial_headless_results_skip_reveal(self) -> None:
        """If headless already collected some results, accept the partial — don't reveal."""
        # Page 1: 2 results. Page 2: captcha → _search returns partial 2.
        headless, _ = _make_browser_mock([CLEAN_HTML, CLEAN_HTML, CAPTCHA_HTML])
        app = _FakeAppContext(headless_browser=headless)

        results = await main.run(app, "q", limit=20)

        assert len(results) == 2
        assert app.reveal_calls == 0
        assert app.hide_calls == 0

    async def test_concurrent_calls_are_serialized_by_request_lock(self) -> None:
        """Two parallel run() calls must not interleave under request_lock.

        Critical under HTTP transport: a CAPTCHA reveal in one request closes
        and relaunches the shared BrowserContext; if a second request had its
        ``page`` mid-flight, the handle would be invalid. Serialization keeps
        each run()'s browser interaction atomic from get_browser() through
        hide_after_captcha().
        """
        active = 0
        peak = 0

        async def tracking_get_browser():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            # Yield so a competing task could run if not blocked by the lock.
            await asyncio.sleep(0.01)
            return browser_mock

        browser_mock, _ = _make_browser_mock(
            [CLEAN_HTML, CLEAN_HTML] * 2  # two run()s, two content() calls each
        )
        app = _FakeAppContext(headless_browser=browser_mock)
        app.get_browser = tracking_get_browser

        async def one_call():
            try:
                return await main.run(app, "q", limit=2)
            finally:
                nonlocal active
                active -= 1

        results = await asyncio.gather(one_call(), one_call())

        assert all(len(r) == 2 for r in results)
        assert peak == 1, f"expected serialized execution; saw peak concurrency {peak}"


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

    async def test_launch_passes_stealth_args_and_user_agent(self) -> None:
        """Every launch must carry the anti-fingerprint flag and pinned UA.

        These are the bits Google's bot detector reads first; if a future edit
        drops one without thinking, CAPTCHA rate quietly spikes — pin them here.
        """
        playwright = MagicMock()
        b1, b2 = MagicMock(), MagicMock()
        b1.close = AsyncMock()
        playwright.chromium.launch_persistent_context = AsyncMock(side_effect=[b1, b2])
        app = main.AppContext(playwright=playwright, user_data_dir="/fake")

        await app.get_browser()
        await app.reveal_for_captcha()

        calls = playwright.chromium.launch_persistent_context.await_args_list
        assert len(calls) == 2
        for call in calls:
            assert "--disable-blink-features=AutomationControlled" in call.kwargs["args"]
            ua = call.kwargs["user_agent"]
            assert "Chrome/" in ua
            assert "Headless" not in ua

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

        results = await main.Web_search("query", ctx, limit=2)

        assert len(results) == 2
        assert results[0]["type"] == "web_search_result"
        assert {"type", "title", "url", "page_content", "page_age"} <= results[0].keys()

    async def test_respects_custom_limit(self) -> None:
        # Page 1: 2 results, Page 2: 2 different results — limit 3 trims to 3.
        headless, _ = _make_browser_mock([CLEAN_HTML, CLEAN_HTML, PAGE2_HTML, PAGE2_HTML])
        app = _FakeAppContext(headless_browser=headless)
        ctx = _ctx_with(app)

        results = await main.Web_search("query", ctx, limit=3)

        assert len(results) == 3

    async def test_default_limit_is_ten(self) -> None:
        sig = inspect.signature(main.Web_search)
        assert sig.parameters["limit"].default == 10

    async def test_invalid_limit_is_clamped_to_one(self) -> None:
        headless, _ = _make_browser_mock([CLEAN_HTML, CLEAN_HTML])
        app = _FakeAppContext(headless_browser=headless)
        ctx = _ctx_with(app)

        # limit=0 should clamp to 1, returning a single result.
        results = await main.Web_search("query", ctx, limit=0)

        assert len(results) == 1

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
