from playwright.sync_api import sync_playwright, Playwright
from time import sleep
from urllib.parse import quote
import os
from dotenv import load_dotenv
load_dotenv()
profile_path = os.getenv("PROFILE_DIR")

def run(playwright: Playwright):
    user_data_dir = profile_path

    chromium = playwright.chromium # or "firefox" or "webkit".
    browser = chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel="chrome", 
        headless=False
        )
    page = browser.new_page()
    query = "как научиться программировать"
    page.goto(f"https://www.google.com/search?q={quote(query)}")
    # other actions...
    print(page.content())
    sleep(20)
    browser.close()

with sync_playwright() as playwright:
    run(playwright)