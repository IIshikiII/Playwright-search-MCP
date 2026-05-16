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
    """
    return os.getenv("PROFILE_DIR", "")
