#!/usr/bin/env python3
"""
smeny.cz shift watcher
=======================
Logs into your smeny.cz employee account, checks the shifts page for
newly-available ("unlocked") shifts, and sends you a WhatsApp message
via CallMeBot whenever a new one shows up.

TWO MODES
---------
1. Discovery mode (run this first, once):
     python watch_shifts.py --discover
   Logs in, saves a screenshot (discover_screenshot.png) and the page
   HTML (discover_page.html) to disk so you can confirm/adjust the CSS
   selectors below. Nothing is sent on WhatsApp in this mode.

2. Normal mode (what runs on a schedule):
     python watch_shifts.py
   Logs in, reads current available shifts, compares them to the last
   known state (state.json), and WhatsApps you about anything new.

CONFIGURATION
--------------
All secrets/config come from environment variables (see .env.example).
The CSS selectors in the SELECTORS block below are my best guess based
on smeny.cz's public documentation/screenshots -- I have not been able
to log into your actual account, so you WILL likely need to adjust
them once using discovery mode. Look for "ADJUST ME" comments.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuration (from environment variables)
# ---------------------------------------------------------------------------

SMENY_EMAIL = os.environ.get("SMENY_EMAIL")
SMENY_PASSWORD = os.environ.get("SMENY_PASSWORD")

# The page that lists your shifts / shift exchange once logged in.
# Log in manually in a normal browser first, click through to the
# "Směny" / shift-exchange tab, and copy the exact URL here.
SHIFTS_URL = os.environ.get("SHIFTS_URL", "https://smeny.cz/app/shifts")

# CallMeBot: message "I allow callmebot to send me messages" to the
# CallMeBot WhatsApp number, it replies with your personal API key.
CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE")  # e.g. "+421900000000"
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY")

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# ---------------------------------------------------------------------------
# Selectors -- ADJUST ME after running --discover
# ---------------------------------------------------------------------------

SELECTORS = {
    # Login form fields on https://smeny.cz/home
    "login_email": 'input[type="email"], input[name="email"]',
    "login_password": 'input[type="password"], input[name="password"]',
    "login_submit": 'button[type="submit"]',
    # A logged-in-only element used to confirm login succeeded.
    "logged_in_marker": '[class*="dashboard"], [class*="calendar"]',
    # Each row/card representing one shift on the shifts page.
    "shift_card": '[class*="shift"]',
    # Within a shift card: the element that marks it as "unlocked"/
    # available to claim (per smeny.cz's FAQ this is a padlock icon).
    "unlocked_marker": '[class*="unlock"], [class*="lock-open"]',
}

LOGIN_URL = "https://smeny.cz/home"


def log(msg: str) -> None:
    print(f"[watch_shifts] {msg}", flush=True)


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        log(f"ERROR: missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


def send_whatsapp(text: str) -> None:
    """Send a WhatsApp message via CallMeBot."""
    if not CALLMEBOT_PHONE or not CALLMEBOT_APIKEY:
        log("CallMeBot not configured, skipping WhatsApp send. Message was:")
        log(text)
        return

    params = {
        "phone": CALLMEBOT_PHONE,
        "text": text,
        "apikey": CALLMEBOT_APIKEY,
    }
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            log(f"CallMeBot response: {body[:200]}")
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR sending WhatsApp message: {exc}")


def load_previous_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def save_state(shift_ids: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps(sorted(shift_ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def snapshot(page, name: str) -> None:
    """Save a screenshot + HTML dump of the current page state for debugging."""
    try:
        page.screenshot(path=f"discover_{name}.png", full_page=True)
        Path(f"discover_{name}.html").write_text(page.content(), encoding="utf-8")
        log(f"Saved discover_{name}.png / discover_{name}.html")
    except Exception as exc:  # noqa: BLE001
        log(f"WARNING: could not save snapshot '{name}': {exc}")


def login(page, debug: bool = False) -> None:
    log(f"Navigating to {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="networkidle")

    if debug:
        snapshot(page, "01_login_page")

    try:
        page.fill(SELECTORS["login_email"], SMENY_EMAIL, timeout=10000)
        page.fill(SELECTORS["login_password"], SMENY_PASSWORD, timeout=10000)
        page.click(SELECTORS["login_submit"], timeout=10000)
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: could not find/fill the login form fields: {exc}")
        if debug:
            snapshot(page, "02_login_form_error")
            log(
                "Login form selectors are wrong for this site. Open "
                "discover_01_login_page.html, find the actual email/password "
                "input fields and the submit button, and update "
                "SELECTORS['login_email'], SELECTORS['login_password'], and "
                "SELECTORS['login_submit'] at the top of this file."
            )
            sys.exit(0)
        raise

    # Give the SPA time to redirect/render the dashboard.
    page.wait_for_load_state("networkidle")

    if debug:
        snapshot(page, "03_after_login_submit")

    try:
        page.wait_for_selector(SELECTORS["logged_in_marker"], timeout=15000)
        log("Login looks successful.")
    except Exception:  # noqa: BLE001
        log(
            "WARNING: could not confirm login succeeded (logged_in_marker not "
            "found). This may be fine -- check discover_03_after_login_submit.png "
            "to see if you're actually logged in; if so, just update "
            "SELECTORS['logged_in_marker'] to match something real on that page."
        )


def collect_available_shifts(page) -> dict[str, str]:
    """
    Returns a dict of {shift_id: human_readable_description} for every
    shift currently marked as available/unlocked.

    shift_id is a stable-ish string derived from the card's text content,
    used only to detect "is this shift new". It doesn't need to be a real
    database ID.
    """
    log(f"Navigating to {SHIFTS_URL}")
    page.goto(SHIFTS_URL, wait_until="networkidle")

    cards = page.query_selector_all(SELECTORS["shift_card"])
    log(f"Found {len(cards)} shift card(s) on the page.")

    available: dict[str, str] = {}
    for card in cards:
        unlocked = card.query_selector(SELECTORS["unlocked_marker"])
        if not unlocked:
            continue
        text = (card.inner_text() or "").strip()
        if not text:
            continue
        # Collapse whitespace, use as both ID and description.
        flat = " ".join(text.split())
        available[flat] = flat

    log(f"Of which {len(available)} appear unlocked/available.")
    return available


def run_discover() -> None:
    require_env("SMENY_EMAIL", "SMENY_PASSWORD")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login(page, debug=True)
        try:
            page.goto(SHIFTS_URL, wait_until="networkidle")
        except Exception as exc:  # noqa: BLE001
            log(f"WARNING: navigating to SHIFTS_URL failed: {exc}")
        snapshot(page, "04_shifts_page")
        browser.close()
    log(
        "Discovery finished. Download the artifact and look through the "
        "discover_01/03/04 .png files to see how far it got, and the "
        "matching .html files for the real element structure."
    )


def run_check() -> None:
    require_env("SMENY_EMAIL", "SMENY_PASSWORD")

    previous = load_previous_state()
    log(f"Loaded {len(previous)} previously-seen available shift(s) from state.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login(page)
        available = collect_available_shifts(page)
        browser.close()

    current_ids = set(available.keys())
    new_ids = current_ids - previous

    if new_ids:
        log(f"{len(new_ids)} new shift(s) found!")
        lines = [available[i] for i in new_ids]
        message = "🟢 Nová volná směna na smeny.cz:\n\n" + "\n\n".join(lines)
        # WhatsApp/CallMeBot messages have a practical length limit.
        if len(message) > 1500:
            message = message[:1500] + "\n... (zkráceno)"
        send_whatsapp(message)
    else:
        log("No new shifts since last check.")

    save_state(current_ids)


if __name__ == "__main__":
    if "--discover" in sys.argv:
        run_discover()
    else:
        run_check()
