"""Create Nexus employees by driving the Settings -> Users form in a browser.

Nexus exposes no API to us, so a record is created the way a person would
create it. There is no OTP step, so the whole run including login is
unattended.

Selectors were taken from the live page (see nexus_inspect.py); they are not
guesses:

  * the three text fields are matched on their placeholders
  * role and shift are custom dropdowns, not <select> — a `button[type=button]`
    toggle with the current value as its text, and an option panel that is
    rendered as the toggle's next sibling only while open
  * the submit button is distinguished from the "Add Employee" button that
    reveals the form by its indigo background

**Why this runs in a child process.** Playwright starts its driver as a
subprocess, and on Windows spawning a subprocess from asyncio needs the
ProactorEventLoop — uvicorn runs a SelectorEventLoop, which raises
NotImplementedError instead. A worker thread does not help: the event loop
policy is process-global, so Playwright's own internal loop comes out as a
selector loop as well. A spawned child process starts with the default
policy and works. It also isolates browser crashes from the API, and matches
where this ends up anyway — a Celery worker, which is a separate process.

Standalone scripts never hit this, because asyncio.run() gets a Proactor
loop; it only shows up under the API, which is exactly where it matters.
"""

import asyncio
import logging
import multiprocessing
import re
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Locator, Page, TimeoutError as PlaywrightTimeout

from app.core.config import settings

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).resolve().parents[2] / ".nexus"
AUTH_FILE = STATE_DIR / "auth.json"

FIELD_TIMEOUT_MS = 15_000
# Short: this only has to outlast the app's own redirect decision.
LOGIN_CHECK_MS = 8_000

ROLE_OPTIONS = ["Admin", "Agent", "Viewer"]
SHIFT_OPTIONS = ["No Shift", "Morning Shift", "Afternoon Shift", "Night Shift"]

# Identifies the two dropdown toggles by whatever value they currently show.
_TOGGLE_TEXT_RE = re.compile(rf"^({'|'.join(ROLE_OPTIONS + SHIFT_OPTIONS)})$")


class NexusError(RuntimeError):
    """Raised when the browser run could not complete the form."""


def _needs_login(page: Page) -> bool:
    """Are we looking at the login form rather than the app?

    An expired session is bounced to /login by the app itself, in JavaScript,
    which happens *after* domcontentloaded — so reading page.url straight
    after a goto misses it and the run then times out on a control that will
    never appear. Waiting for the password field is what actually detects it.
    """
    try:
        page.locator("input[type='password']").first.wait_for(timeout=LOGIN_CHECK_MS)
        return True
    except PlaywrightTimeout:
        return False


def _login(page: Page) -> None:
    if not settings.NEXUS_USERNAME or not settings.NEXUS_PASSWORD:
        raise NexusError("NEXUS_USERNAME / NEXUS_PASSWORD are not set — add them to Horizon-Api's .env")

    logger.info("Nexus: logging in as %s", settings.NEXUS_USERNAME)
    page.goto(f"{settings.NEXUS_BASE_URL}/login", wait_until="domcontentloaded")
    page.locator("input[type='email']").first.fill(settings.NEXUS_USERNAME)
    page.locator("input[type='password']").first.fill(settings.NEXUS_PASSWORD)
    page.get_by_role("button", name="Sign In").first.click()
    page.wait_for_url("**/dashboard**", timeout=30_000)


def _choose(toggle: Locator, value: str) -> None:
    """Pick `value` from one of the custom dropdowns.

    The options exist in the DOM only while the panel is open, so the toggle
    has to be clicked first; the panel is its immediately following sibling.
    """
    toggle.click(timeout=FIELD_TIMEOUT_MS)
    panel = toggle.locator("xpath=following-sibling::*[1]")
    panel.get_by_text(value, exact=True).first.click(timeout=FIELD_TIMEOUT_MS)
    # Let the panel close before touching the next control, so its overlay
    # cannot swallow the following click.
    panel.wait_for(state="hidden", timeout=FIELD_TIMEOUT_MS)


def _add_employee_sync(data: dict[str, Any], dry_run: bool, headless: bool) -> dict:
    STATE_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    role = data.get("role", "Agent")
    shift = data.get("shift", "No Shift")
    if role not in ROLE_OPTIONS:
        raise NexusError(f"Unknown role {role!r} — Nexus offers {ROLE_OPTIONS}")
    if shift not in SHIFT_OPTIONS:
        raise NexusError(f"Unknown shift {shift!r} — Nexus offers {SHIFT_OPTIONS}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=str(AUTH_FILE) if AUTH_FILE.exists() else None,
            viewport={"width": 1600, "height": 1000},
        )
        page = context.new_page()

        try:
            page.goto(f"{settings.NEXUS_BASE_URL}/dashboard/settings", wait_until="domcontentloaded")
            if _needs_login(page):
                _login(page)
                page.goto(f"{settings.NEXUS_BASE_URL}/dashboard/settings", wait_until="domcontentloaded")

            # Reveal the New Employee fields — they are not rendered until the
            # page's own "Add Employee" button is pressed.
            page.get_by_role("button", name="Add Employee").first.click(timeout=FIELD_TIMEOUT_MS)

            full_name = page.locator("input[placeholder='Full Name']")
            full_name.wait_for(timeout=FIELD_TIMEOUT_MS)
            full_name.fill(data["full_name"])
            page.locator("input[placeholder='Email Address']").fill(data["email"])
            page.locator("input[placeholder='Password']").fill(data["password"])

            # The two dropdown toggles are the only buttons whose entire text
            # is one of the role/shift values — the same values also appear in
            # the employee table below, but those are spans, not buttons.
            toggles = page.locator("button[type='button']").filter(has_text=_TOGGLE_TEXT_RE)
            _choose(toggles.nth(0), role)
            # Re-resolve: the role toggle's text has just changed, but it still
            # matches, so the shift toggle stays at index 1.
            toggles = page.locator("button[type='button']").filter(has_text=_TOGGLE_TEXT_RE)
            _choose(toggles.nth(1), shift)

            shot = STATE_DIR / f"filled-{stamp}.png"
            page.screenshot(path=str(shot), full_page=True)

            if dry_run:
                logger.info("Nexus: dry run — form filled, not submitted")
                return {"submitted": False, "screenshot": str(shot), "url": page.url}

            page.locator("button.bg-indigo-600").filter(has_text="Add Employee").first.click(
                timeout=FIELD_TIMEOUT_MS
            )
            page.wait_for_timeout(3000)
            context.storage_state(path=str(AUTH_FILE))

            after = STATE_DIR / f"saved-{stamp}.png"
            page.screenshot(path=str(after), full_page=True)
            logger.info("Nexus: employee submitted (%s)", data["email"])
            return {"submitted": True, "screenshot": str(after), "url": page.url}

        except PlaywrightTimeout as exc:
            fail = STATE_DIR / f"error-{stamp}.png"
            page.screenshot(path=str(fail), full_page=True)
            (STATE_DIR / f"error-{stamp}.html").write_text(page.content(), encoding="utf-8")
            logger.error("Nexus: timed out on %s — see %s", page.url, fail)
            raise NexusError(f"Timed out on the Nexus form at {page.url} — see {fail}") from exc
        finally:
            browser.close()


def _run_in_child(data: dict[str, Any], dry_run: bool, headless: bool) -> dict:
    """Entry point for the spawned process.

    NexusError does not survive pickling cleanly across the process boundary,
    so failures come back as a plain flag the caller re-raises.
    """
    try:
        return _add_employee_sync(data, dry_run, headless)
    except NexusError as exc:
        return {"error": str(exc)}


async def add_employee(data: dict[str, Any], *, dry_run: bool = True, headless: bool = True) -> dict:
    """Create one employee in Nexus by filling the New Employee form.

    With `dry_run=True` the form is filled but never submitted, so a run can be
    verified without creating a user.

    The browser work happens in a child process — see the module docstring for
    why it cannot run on uvicorn's event loop.
    """
    loop = asyncio.get_running_loop()
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        result = await loop.run_in_executor(pool, _run_in_child, data, dry_run, headless)

    if "error" in result:
        raise NexusError(result["error"])
    return result
