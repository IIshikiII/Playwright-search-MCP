"""pytest configuration and fixtures for plsearch tests."""

import sys

import pytest

# Match main.py's Windows console encoding fix so tests can print non-ASCII
# (e.g., the CAPTCHA warning emoji in wait_for_human_resolution) without
# UnicodeEncodeError on cp1251-default consoles. pyproject.toml pins -s, so
# stdout isn't captured by pytest and writes go straight to the terminal.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def pytest_addoption(parser: "pytest.Parser") -> None:
    """Add command line options for pytest."""
    parser.addoption(
        "--slow",
        action="store_true",
        default=False,
        help="Run slow integration tests (requires visible browser for CAPTCHA)"
    )


def pytest_configure(config: "pytest.Config") -> None:
    """Register custom pytest markers."""
    config.addinivalue_line(
        "markers", "slow: Mark tests as slow (require --slow flag for CAPTCHA handling)"
    )


def pytest_collection_modifyitems(
    config: "pytest.Config", items: list["pytest.Item"]
) -> None:
    """Skip slow tests unless --slow is passed."""
    if not config.getoption("--slow"):
        skip_slow = pytest.mark.skip(
            reason="Slow test, use --slow to run"
        )
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip_slow)
