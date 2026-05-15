"""Configuration module for plsearch."""

import os
from pathlib import Path

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

# Logging
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "search.log"


def get_profile_path() -> str:
    """Get the Chrome user data directory path from environment variables.

    Returns:
        str: Path to the Chrome user data directory.
    """
    return os.getenv("PROFILE_DIR", "")
