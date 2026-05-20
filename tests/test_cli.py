"""Unit tests for plsearch.cli.client."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plsearch.cli import client
from plsearch.cli.client import (
    EXIT_OK,
    EXIT_TOOL_ERROR,
    EXIT_TRANSPORT_ERROR,
    _build_parser,
    _format_payload,
    _resolve_url,
    _unwrap_structured,
    main,
)


SAMPLE_RESULTS = [
    {
        "type": "web_search_result",
        "title": "Result A",
        "url": "https://example.com/a",
        "page_content": "snippet A",
        "page_age": "",
    }
]


def _fake_streams() -> SimpleNamespace:
    """Return a stand-in for the (read, write, get_session_id) tuple."""
    return SimpleNamespace(read=MagicMock(), write=MagicMock(), get_id=MagicMock())


def _patch_transport(monkeypatch: pytest.MonkeyPatch, session: MagicMock) -> None:
    """Wire fakes for streamable_http_client + ClientSession so _call_server runs."""
    streams = _fake_streams()

    @asynccontextmanager
    async def fake_transport(url, **_kwargs):
        yield (streams.read, streams.write, streams.get_id)

    @asynccontextmanager
    async def fake_session(read, write):
        yield session

    monkeypatch.setattr(client, "streamable_http_client", fake_transport)
    monkeypatch.setattr(client, "ClientSession", fake_session)


class TestResolveUrl:
    def test_uses_config_defaults_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PLSEARCH_HOST", raising=False)
        monkeypatch.delenv("PLSEARCH_PORT", raising=False)
        assert _resolve_url(None, None) == "http://127.0.0.1:8765/mcp"

    def test_non_wildcard_env_propagates_through_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PLSEARCH_HOST", "192.168.1.50")
        monkeypatch.setenv("PLSEARCH_PORT", "9090")
        assert _resolve_url(None, None) == "http://192.168.1.50:9090/mcp"

    def test_explicit_flags_beat_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PLSEARCH_HOST", "0.0.0.0")
        monkeypatch.setenv("PLSEARCH_PORT", "9090")
        assert _resolve_url("10.0.0.5", 1234) == "http://10.0.0.5:1234/mcp"

    def test_wildcard_env_maps_to_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Server is commonly bound to 0.0.0.0 (listen on all interfaces);
        # the CLI must connect via 127.0.0.1 — Windows `connect(0.0.0.0)`
        # errors with WinError 10049, and Linux silently maps to loopback.
        # Port must propagate untouched.
        monkeypatch.setenv("PLSEARCH_HOST", "0.0.0.0")
        monkeypatch.setenv("PLSEARCH_PORT", "9090")
        assert _resolve_url(None, None) == "http://127.0.0.1:9090/mcp"

    def test_wildcard_flag_also_maps_to_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An explicit --host 0.0.0.0 is just as broken as the env one;
        # connecting to 0.0.0.0 is never the right answer, so map it too.
        monkeypatch.delenv("PLSEARCH_HOST", raising=False)
        monkeypatch.delenv("PLSEARCH_PORT", raising=False)
        assert _resolve_url("0.0.0.0", None) == "http://127.0.0.1:8765/mcp"


class TestUnwrapStructured:
    def test_returns_none_for_none(self) -> None:
        assert _unwrap_structured(None) is None

    def test_unwraps_single_result_key(self) -> None:
        assert _unwrap_structured({"result": [1, 2, 3]}) == [1, 2, 3]

    def test_leaves_multi_key_dicts_alone(self) -> None:
        payload = {"result": [1], "extra": "x"}
        assert _unwrap_structured(payload) == payload

    def test_leaves_unrelated_single_key_dicts_alone(self) -> None:
        payload = {"other": 42}
        assert _unwrap_structured(payload) == payload


class TestFormatPayload:
    def test_prefers_structured_content(self) -> None:
        result = SimpleNamespace(
            structuredContent={"result": SAMPLE_RESULTS},
            content=[SimpleNamespace(text="ignored")],
        )
        parsed = json.loads(_format_payload(result))
        assert parsed == SAMPLE_RESULTS

    def test_falls_back_to_text_content_when_no_structured(self) -> None:
        result = SimpleNamespace(
            structuredContent=None,
            content=[SimpleNamespace(text=json.dumps(SAMPLE_RESULTS))],
        )
        parsed = json.loads(_format_payload(result))
        assert parsed == SAMPLE_RESULTS

    def test_emits_raw_text_when_content_is_not_json(self) -> None:
        result = SimpleNamespace(
            structuredContent=None,
            content=[SimpleNamespace(text="just a string, sorry")],
        )
        # json.dumps of a string returns a quoted JSON string literal.
        assert json.loads(_format_payload(result)) == "just a string, sorry"


class TestArgparse:
    def test_defaults(self) -> None:
        args = _build_parser().parse_args(["python asyncio"])
        assert args.query == "python asyncio"
        assert args.limit == 10
        assert args.host is None
        assert args.port is None

    def test_overrides(self) -> None:
        args = _build_parser().parse_args(
            ["foo", "--limit", "3", "--host", "1.2.3.4", "--port", "9999"]
        )
        assert args.limit == 3
        assert args.host == "1.2.3.4"
        assert args.port == 9999

    def test_missing_query_errors(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args([])


class TestMainHappyPath:
    def test_prints_results_and_exits_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        session = MagicMock()
        session.initialize = AsyncMock()
        session.call_tool = AsyncMock(
            return_value=SimpleNamespace(
                isError=False,
                structuredContent={"result": SAMPLE_RESULTS},
                content=[],
            )
        )
        _patch_transport(monkeypatch, session)

        rc = main(["python asyncio", "--limit", "1"])

        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert json.loads(out) == SAMPLE_RESULTS
        session.call_tool.assert_awaited_once_with(
            "Web_search",
            {"state": "python asyncio", "limit": 1},
            read_timeout_seconds=session.call_tool.call_args.kwargs["read_timeout_seconds"],
        )

    def test_clamps_zero_limit_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MagicMock()
        session.initialize = AsyncMock()
        session.call_tool = AsyncMock(
            return_value=SimpleNamespace(
                isError=False,
                structuredContent={"result": []},
                content=[],
            )
        )
        _patch_transport(monkeypatch, session)

        main(["q", "--limit", "0"])

        call = session.call_tool.await_args
        assert call.args[1]["limit"] == 1


class TestMainToolError:
    def test_returns_exit_two_and_prints_to_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        session = MagicMock()
        session.initialize = AsyncMock()
        session.call_tool = AsyncMock(
            return_value=SimpleNamespace(
                isError=True,
                structuredContent=None,
                content=[SimpleNamespace(text="boom")],
            )
        )
        _patch_transport(monkeypatch, session)

        rc = main(["q"])

        captured = capsys.readouterr()
        assert rc == EXIT_TOOL_ERROR
        assert captured.out == ""
        assert "tool returned an error" in captured.err
        assert "boom" in captured.err


class TestMainTransportError:
    def test_connection_refused_returns_exit_one_with_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        @asynccontextmanager
        async def boom(url, **_kwargs):
            raise ConnectionRefusedError("nothing on 8765")
            yield  # pragma: no cover — make this a generator

        monkeypatch.setattr(client, "streamable_http_client", boom)

        rc = main(["q", "--host", "127.0.0.1", "--port", "8765"])

        captured = capsys.readouterr()
        assert rc == EXIT_TRANSPORT_ERROR
        assert captured.out == ""
        assert "http://127.0.0.1:8765/mcp" in captured.err
        assert "uv run python -m plsearch.main" in captured.err
