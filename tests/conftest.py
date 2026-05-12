"""pytest configuration and fixtures for plsearch tests."""

import pytest


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
