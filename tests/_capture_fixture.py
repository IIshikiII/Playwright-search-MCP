"""One-off utility: capture a real Google results page into tests/fixtures/.

Run with `uv run python tests/_capture_fixture.py`. Opens a visible Chrome via
the project's persistent profile, navigates to Google, waits up to 120s for
manual CAPTCHA solving if challenged, and writes the post-load HTML to
``tests/fixtures/google_results.html``.

Pytest skips collecting this file (leading underscore). It exists to keep the
real-fixture test cheap to regenerate when Google's DOM shifts.
"""

import asyncio
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

from plsearch.config import (
    GOOGLE_SEARCH_URL,
    get_profile_path,
    is_captcha_page,
    wait_until_captcha_solved,
)

OUT_PATH = Path(__file__).parent / "fixtures" / "google_results.html"
QUERY = "python playwright tutorial"
CAPTCHA_TIMEOUT_SECONDS = 120.0


async def capture(query: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=get_profile_path(),
            channel="chrome",
            headless=False,
        )
        page = await ctx.new_page()
        await page.goto(f"{GOOGLE_SEARCH_URL}{quote(query)}")

        content = await page.content()
        if is_captcha_page(content):
            print(
                f"CAPTCHA detected. Solve it in the visible browser within "
                f"{CAPTCHA_TIMEOUT_SECONDS:.0f}s..."
            )
            if not await wait_until_captcha_solved(page, timeout=CAPTCHA_TIMEOUT_SECONDS):
                raise RuntimeError("CAPTCHA was not solved within timeout")

        await page.wait_for_load_state("domcontentloaded")
        content = await page.content()
        out_path.write_text(content, encoding="utf-8")
        print(f"Saved {len(content):,} bytes to {out_path}")

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(capture(QUERY, OUT_PATH))
