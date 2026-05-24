"""
One-time helper: export your real TikTok browser session cookies to
data/tiktok_cookies.json so the paid ad library scraper can inject them.

Why this helps:
  TikTok's bot scoring heavily penalises cold (no-cookie) sessions.
  A real session cookie carries trust built up from genuine browsing history.
  The scraper only READS these cookies — it never logs in or posts anything.

How to use:
  1. Open TikTok in your normal Chrome browser and make sure you are logged in
     (or at minimum have visited library.tiktok.com recently)
  2. Run:  python tools/export_tiktok_cookies.py
  3. A Chrome window will open showing library.tiktok.com
  4. If prompted, log in with your TikTok account
  5. Once the page loads fully, press Enter in the terminal
  6. Cookies are saved to data/tiktok_cookies.json
  7. Close the browser window

The cookie file contains session tokens — keep it private.
It is already listed in .gitignore.

Cookies expire after ~30–90 days. Re-run this script when TikTok scraping
starts returning empty results again.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


async def export_cookies():
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        print("ERROR: patchright not installed. Run: pip install patchright")
        return

    output_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "../data/tiktok_cookies.json")
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    print("\n── TikTok Cookie Exporter ──────────────────────────────────────")
    print("A browser window will open. Log in to TikTok if prompted.")
    print("Once library.tiktok.com has fully loaded, come back here and")
    print("press Enter to save cookies.\n")

    async with async_playwright() as p:
        # Launch visible (non-headless) so you can interact
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()
        await page.goto("https://library.tiktok.com/", wait_until="domcontentloaded")

        input("Press Enter once library.tiktok.com has fully loaded... ")

        cookies = await context.cookies(["https://library.tiktok.com", "https://www.tiktok.com"])

        # Only keep tiktok.com domain cookies — nothing else
        safe_cookies = [
            c for c in cookies
            if "tiktok.com" in c.get("domain", "")
        ]

        await browser.close()

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(safe_cookies, f, indent=2)

    print(f"\n✓ Saved {len(safe_cookies)} cookies to {output_path}")
    print("  The scraper will use these automatically on next run.\n")


if __name__ == "__main__":
    asyncio.run(export_cookies())
