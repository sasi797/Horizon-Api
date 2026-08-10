"""Discovery script for the Nexus "New Employee" form.

Logs in, opens Settings -> Users, reveals the Add Employee form and dumps its
markup so real selectors can be written for nexus_sync.py. Creates nothing.

    set NEXUS_USERNAME=... && set NEXUS_PASSWORD=...
    ./.venv/Scripts/python.exe nexus_inspect.py
"""

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

BASE_URL = os.getenv("NEXUS_BASE_URL", "https://nexus.linkworks.in")
USERNAME = os.getenv("NEXUS_USERNAME", "")
PASSWORD = os.getenv("NEXUS_PASSWORD", "")

OUT_DIR = Path(__file__).parent / ".nexus"


async def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 1000})
        page = await context.new_page()

        await page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
        (OUT_DIR / "login.html").write_text(await page.content(), encoding="utf-8")

        await page.locator("input[type='email'], input[name='email']").first.fill(USERNAME)
        await page.locator("input[type='password']").first.fill(PASSWORD)
        await page.get_by_role("button", name="Sign In").first.click()
        await page.wait_for_url("**/dashboard**", timeout=30_000)
        print(f"Logged in — landed on {page.url}")

        await context.storage_state(path=str(OUT_DIR / "auth.json"))

        await page.goto(f"{BASE_URL}/dashboard/settings", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # The New Employee fields only render after this button is clicked.
        try:
            await page.get_by_role("button", name="Add Employee").first.click(timeout=10_000)
            await page.wait_for_timeout(1000)
            print("Clicked 'Add Employee'")
        except Exception as exc:
            print(f"Could not click 'Add Employee': {exc}")

        (OUT_DIR / "settings_users.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT_DIR / "settings_users.png"), full_page=True)
        print(f"Wrote HTML + screenshot to {OUT_DIR}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
