"""Unit tests for plsearch.config helpers."""

import pytest

from plsearch import config
from plsearch.config import (
    CAPTCHA_FORM_ID,
    DEFAULT_HOST,
    DEFAULT_PORT,
    RECAPTCHA_ID,
    cleanup_stale_profile_locks,
    get_http_host,
    get_http_port,
    get_profile_path,
    is_captcha_page,
    wait_until_captcha_solved,
)


class _ScriptedPage:
    """Test double exposing async `content()` that yields scripted values.

    Each scripted item is either a string (returned) or an Exception subclass
    (raised) — lets us cover transient-error retry paths cleanly.
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)

    async def content(self) -> str:
        if not self._script:
            raise AssertionError("Page.content() called more times than scripted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestIsCaptchaPage:
    def test_detects_captcha_form_id(self) -> None:
        html = f'<html><body><form id="{CAPTCHA_FORM_ID}"></form></body></html>'
        assert is_captcha_page(html) is True

    def test_detects_recaptcha_id(self) -> None:
        html = f'<html><body><div id="{RECAPTCHA_ID}"></div></body></html>'
        assert is_captcha_page(html) is True

    def test_detects_when_both_markers_present(self) -> None:
        html = f'{CAPTCHA_FORM_ID}-something-{RECAPTCHA_ID}'
        assert is_captcha_page(html) is True

    def test_returns_false_when_neither_marker_present(self) -> None:
        html = "<html><body><h1>Search results</h1></body></html>"
        assert is_captcha_page(html) is False

    def test_returns_false_on_empty_string(self) -> None:
        assert is_captcha_page("") is False


class TestGetProfilePath:
    def test_returns_env_value_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROFILE_DIR", "/some/profile/path")
        assert get_profile_path() == "/some/profile/path"

    def test_raises_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROFILE_DIR", raising=False)
        with pytest.raises(RuntimeError, match="PROFILE_DIR is required"):
            get_profile_path()

    def test_raises_when_explicitly_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROFILE_DIR", "")
        with pytest.raises(RuntimeError, match="PROFILE_DIR is required"):
            get_profile_path()


class TestWaitUntilCaptchaSolved:
    async def test_returns_true_when_captcha_clears(self) -> None:
        page = _ScriptedPage([
            f'has {CAPTCHA_FORM_ID} here',
            f'still {RECAPTCHA_ID}',
            "<html>clean results</html>",
        ])
        assert await wait_until_captcha_solved(page, timeout=5.0, poll_interval=0.001) is True

    async def test_returns_false_when_timeout_elapses(self) -> None:
        # Long script of CAPTCHA pages — function should give up on its own deadline.
        page = _ScriptedPage([f"<form id='{CAPTCHA_FORM_ID}'>"] * 50)
        assert await wait_until_captcha_solved(page, timeout=0.05, poll_interval=0.001) is False

    async def test_transient_content_error_does_not_abort(self) -> None:
        page = _ScriptedPage([
            RuntimeError("page is navigating"),
            f'still {CAPTCHA_FORM_ID}',
            "<html>clean</html>",
        ])
        assert await wait_until_captcha_solved(page, timeout=5.0, poll_interval=0.001) is True

    async def test_returns_true_immediately_when_no_captcha(self) -> None:
        page = _ScriptedPage(["<html>nothing to see here</html>"])
        assert await wait_until_captcha_solved(page, timeout=5.0, poll_interval=0.001) is True


class TestCleanupStaleProfileLocks:
    def test_unlinks_existing_singleton_files(self, tmp_path) -> None:
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (tmp_path / name).write_text("stale")

        cleanup_stale_profile_locks(str(tmp_path))

        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            assert not (tmp_path / name).exists()

    def test_leaves_unrelated_files_alone(self, tmp_path) -> None:
        (tmp_path / "Preferences").write_text("user-data")
        (tmp_path / "SingletonLock").write_text("stale")

        cleanup_stale_profile_locks(str(tmp_path))

        assert (tmp_path / "Preferences").exists()
        assert not (tmp_path / "SingletonLock").exists()

    def test_tolerates_missing_dir(self, tmp_path) -> None:
        cleanup_stale_profile_locks(str(tmp_path / "nope"))  # no raise

    def test_tolerates_missing_files(self, tmp_path) -> None:
        cleanup_stale_profile_locks(str(tmp_path))  # no raise


class TestHttpSettings:
    def test_default_host_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLSEARCH_HOST", raising=False)
        assert get_http_host() == DEFAULT_HOST

    def test_host_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLSEARCH_HOST", "0.0.0.0")
        assert get_http_host() == "0.0.0.0"

    def test_default_port_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLSEARCH_PORT", raising=False)
        assert get_http_port() == DEFAULT_PORT

    def test_port_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLSEARCH_PORT", "9090")
        assert get_http_port() == 9090

    def test_invalid_port_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLSEARCH_PORT", "not-a-number")
        assert get_http_port() == DEFAULT_PORT

    def test_out_of_range_port_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLSEARCH_PORT", "99999")
        assert get_http_port() == DEFAULT_PORT
