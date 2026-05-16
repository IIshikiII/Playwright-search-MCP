"""Configuration module for plsearch."""

import asyncio
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Google Search URL
GOOGLE_SEARCH_URL = "https://www.google.com/search?q="

# CAPTCHA selectors
CAPTCHA_FORM_ID = "captcha-form"
RECAPTCHA_ID = "recaptcha"

# Google result-page selectors
SNIPPET_CLASS = "VwiC3b"

# Google pagination: results-per-page is what Google ships by default; the
# walker advances `start` by this amount per page (start=0, 10, 20, ...).
# MAX_PAGES caps the walk — past page 10 Google's relevance drops off a cliff
# and CAPTCHA frequency spikes, so there's little point chasing further.
RESULTS_PER_PAGE = 10
MAX_PAGES = 10

# HTTP transport settings. Defaults bind to loopback only — exposing the
# server beyond 127.0.0.1 needs auth (FastMCP supports OAuth/token), which
# this project doesn't configure. Override via PLSEARCH_HOST / PLSEARCH_PORT.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def get_http_host() -> str:
    """Return the bind host for the HTTP server (env override or default)."""
    return os.getenv("PLSEARCH_HOST", DEFAULT_HOST)


def get_http_port() -> int:
    """Return the bind port for the HTTP server (env override or default).

    Invalid PLSEARCH_PORT values fall back to ``DEFAULT_PORT`` with a log
    line — silently using the default would mask a typo in the operator's
    env file.
    """
    raw = os.getenv("PLSEARCH_PORT")
    if raw is None:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        logger.warning("PLSEARCH_PORT=%r is not an integer — using %d", raw, DEFAULT_PORT)
        return DEFAULT_PORT
    if not (1 <= port <= 65535):
        logger.warning("PLSEARCH_PORT=%d out of range — using %d", port, DEFAULT_PORT)
        return DEFAULT_PORT
    return port


def is_captcha_page(page_content: str) -> bool:
    """Check if page contains a Google reCAPTCHA challenge."""
    return CAPTCHA_FORM_ID in page_content or RECAPTCHA_ID in page_content


async def wait_until_captcha_solved(
    page,
    *,
    timeout: float = 120.0,
    poll_interval: float = 1.0,
) -> bool:
    """Poll the page until the CAPTCHA challenge disappears.

    Returns True if the page cleared within `timeout` seconds, False if the
    deadline passed first. Transient errors from ``page.content()`` (e.g., the
    page is mid-navigation) are logged at DEBUG and retried.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            content = await page.content()
            if not is_captcha_page(content):
                return True
        except Exception as e:
            logger.debug("Page content not available yet: %s", e)
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_interval)

# Logging
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "search.log"


def get_profile_path() -> str:
    """Get the Chrome user data directory path from environment variables.

    Returns:
        str: Path to the Chrome user data directory.

    Raises:
        RuntimeError: If ``PROFILE_DIR`` is unset or empty. Without it Playwright
            falls back to a temp dir and Google's CAPTCHA frequency spikes —
            failing fast with a clear message beats an opaque downstream crash.
    """
    value = os.getenv("PROFILE_DIR", "")
    if not value:
        raise RuntimeError("PROFILE_DIR is required (set it in .env)")
    return value


_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def cleanup_stale_profile_locks(user_data_dir: str) -> None:
    """Remove Chrome's Singleton* files left behind by a killed process.

    Chrome creates these on launch and removes them on clean shutdown. Their
    presence means a prior Chrome was killed mid-flight; if we don't sweep
    them first, the next launch refuses to start.

    Process-level cleanup (killing the orphaned Chrome that left these
    behind) is the SessionRegistry's job — see ``session.SessionRegistry``.
    By the time this runs, the owning process is already dead.
    """
    base = Path(user_data_dir)
    if not base.exists():
        return
    for name in _SINGLETON_FILES:
        target = base / name
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("Could not remove %s: %s", target, exc)
