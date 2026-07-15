#!/usr/bin/env python3
"""
smeny.cz shift watcher
=======================
Logs into your smeny.cz employee account, checks for newly-available
("unlocked") shifts using smeny.cz's own internal JSON API, and sends
you a WhatsApp message via CallMeBot whenever a new one shows up.

No browser automation needed -- this uses plain HTTP requests against
the same endpoints the smeny.cz website itself calls:
  1. GET  /home                       -> grab the login CSRF token
  2. POST /login_check                -> log in, get a session cookie
  3. GET  /calendar                   -> read the numeric user ID
  4. GET  /shift/user-list/<user_id>  -> the actual shift data (JSON),
                                          each shift has an "unlocked"
                                          boolean flag

RUN MODES
---------
  python watch_shifts.py            Normal check (compares to state.json,
                                     sends WhatsApp for anything new).
  python watch_shifts.py --discover Prints out what it found without
                                     sending WhatsApp or saving state --
                                     useful for a dry run / debugging.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration (from environment variables)
# ---------------------------------------------------------------------------

SMENY_EMAIL = os.environ.get("SMENY_EMAIL")
SMENY_PASSWORD = os.environ.get("SMENY_PASSWORD")

# How many days ahead to check for available shifts.
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "60"))

# CallMeBot: message "I allow callmebot to send me messages" to the
# CallMeBot WhatsApp number, it replies with your personal API key.
CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE")  # e.g. "+421900000000"
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY")

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# When this repo is public, GitHub Actions logs are visible to anyone.
# By default we keep the console output generic (counts only) and only
# put actual shift details (title, date, time) into the private WhatsApp
# message. Set SHOW_DETAILS_IN_LOGS=true as a repo secret/env var if you
# ever want full detail in the logs too (e.g. while the repo is private).
SHOW_DETAILS_IN_LOGS = os.environ.get("SHOW_DETAILS_IN_LOGS", "false").lower() == "true"

BASE_URL = "https://smeny.cz"
LOGIN_PAGE_URL = f"{BASE_URL}/home"
LOGIN_POST_URL = f"{BASE_URL}/login_check"
CALENDAR_URL = f"{BASE_URL}/calendar"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

CSRF_RE = re.compile(r'name="_csrf_token"\s+value="([^"]+)"')
USER_ID_RE = re.compile(r"var\s+user\s*=\s*\{\s*id:\s*'(\d+)'")


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
    url = f"https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if SHOW_DETAILS_IN_LOGS:
                log(f"CallMeBot response: {body[:200]}")
            else:
                log(f"CallMeBot response received (status {resp.status}).")
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR sending WhatsApp message.")


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


def login() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    log(f"Fetching login page: {LOGIN_PAGE_URL}")
    resp = session.get(LOGIN_PAGE_URL, timeout=30)
    resp.raise_for_status()

    match = CSRF_RE.search(resp.text)
    if not match:
        log("ERROR: could not find _csrf_token on the login page. The login "
            "form may have changed -- inspect the page HTML manually.")
        sys.exit(1)
    csrf_token = match.group(1)

    log("Submitting login form")
    resp = session.post(
        LOGIN_POST_URL,
        data={
            "_username": SMENY_EMAIL,
            "_password": SMENY_PASSWORD,
            "_csrf_token": csrf_token,
        },
        timeout=30,
    )
    resp.raise_for_status()

    if "_username" in resp.text and "_password" in resp.text and "login-form" in resp.text:
        log("ERROR: login appears to have failed (still seeing the login "
            "form after submitting). Double-check SMENY_EMAIL / "
            "SMENY_PASSWORD secrets.")
        sys.exit(1)

    log("Login looks successful.")
    return session


def get_user_id(session: requests.Session) -> str:
    log(f"Fetching {CALENDAR_URL} to read the user ID")
    resp = session.get(CALENDAR_URL, timeout=30)
    resp.raise_for_status()

    match = USER_ID_RE.search(resp.text)
    if not match:
        log("ERROR: could not find the user ID on the calendar page. "
            "smeny.cz may have changed how it embeds this -- inspect "
            "the page HTML manually (look for 'var user = { id:').")
        sys.exit(1)

    user_id = match.group(1)
    if SHOW_DETAILS_IN_LOGS:
        log(f"Found user ID: {user_id}")
    else:
        log("Found user ID (hidden -- set SHOW_DETAILS_IN_LOGS=true to show).")
    return user_id


def fetch_shifts(session: requests.Session, user_id: str) -> list[dict]:
    start = date.today() - timedelta(days=1)
    end = date.today() + timedelta(days=LOOKAHEAD_DAYS)
    url = f"{BASE_URL}/shift/user-list/{user_id}"
    params = {"start": start.isoformat(), "end": end.isoformat()}

    log(f"Fetching shifts from {start} to {end}")
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()

    try:
        shifts = resp.json()
    except ValueError:
        log("ERROR: shift list response was not valid JSON. smeny.cz may "
            "have changed this endpoint -- inspect the raw response.")
        sys.exit(1)

    log(f"Fetched {len(shifts)} shift entries in total.")
    return shifts


WEEKDAY_NAMES_SK = ["Po", "Ut", "St", "Št", "Pi", "So", "Ne"]


def format_datetime(raw: str) -> tuple[str, str]:
    """Turn '2026-07-16 14:00' into ('Št 16.7.2026', '14:00')."""
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return raw, ""
    weekday = WEEKDAY_NAMES_SK[dt.weekday()]
    return f"{weekday} {dt.day}.{dt.month}.{dt.year}", dt.strftime("%H:%M")


def describe_shift(shift: dict) -> str:
    title = shift.get("title", "Neznáma smena")
    start_raw = shift.get("start", "?")
    end_raw = shift.get("end", "?")

    start_date, start_time = format_datetime(start_raw)
    end_date, end_time = format_datetime(end_raw)

    if start_date == end_date:
        when = f"{start_date}, {start_time} – {end_time}"
    else:
        when = f"{start_date} {start_time} – {end_date} {end_time}"

    return f"{title}\n{when}"


def run(discover: bool = False) -> None:
    require_env("SMENY_EMAIL", "SMENY_PASSWORD")

    session = login()
    user_id = get_user_id(session)
    shifts = fetch_shifts(session, user_id)

    available = {str(s["id"]): describe_shift(s) for s in shifts if s.get("unlocked")}
    log(f"Of which {len(available)} are currently unlocked/available.")

    if discover:
        if available:
            if SHOW_DETAILS_IN_LOGS:
                log("Available shifts found:")
                for shift_id, desc in available.items():
                    log(f"  [{shift_id}] {desc}")
            else:
                log(
                    f"{len(available)} available shift(s) found -- details hidden "
                    "from logs. Set SHOW_DETAILS_IN_LOGS=true to show them here, "
                    "or send yourself a test WhatsApp via normal (non-discover) mode."
                )
        else:
            log("No available shifts right now. Re-run this periodically, "
                "or once you know a shift is open, to confirm the "
                "'unlocked' field behaves as expected.")
        return

    previous = load_previous_state()
    log(f"Loaded {len(previous)} previously-seen available shift(s) from state.")

    current_ids = set(available.keys())
    new_ids = current_ids - previous

    if new_ids:
        log(f"{len(new_ids)} new shift(s) found!")
        lines = [available[i] for i in new_ids]
        message = "🟢 Nová volná smena na smeny.cz:\n\n" + "\n\n".join(lines)
        if len(message) > 1500:
            message = message[:1500] + "\n... (skrátené)"
        send_whatsapp(message)
    else:
        log("No new shifts since last check.")

    save_state(current_ids)


if __name__ == "__main__":
    run(discover="--discover" in sys.argv)
