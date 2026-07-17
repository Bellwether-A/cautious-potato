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
  python watch_shifts.py               Normal check (compares to state.json,
                                        sends WhatsApp for anything new).
  python watch_shifts.py --discover    Prints out what it found without
                                        sending WhatsApp or saving state --
                                        useful for a dry run / debugging.
  python watch_shifts.py --test-whatsapp
                                        Sends a canned test WhatsApp message
                                        via CallMeBot only -- does NOT log
                                        into smeny.cz at all. Use this to
                                        confirm CALLMEBOT_PHONE/APIKEY work
                                        on their own, independent of
                                        anything smeny.cz-related.
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
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "config.json"))

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


def load_config() -> dict:
    """Load auto-pick rules from config.json (see config.html to generate one).
    Missing/invalid config is treated as 'no rules' -- i.e. auto-pick is off
    until you've deliberately configured it."""
    if not CONFIG_FILE.exists():
        return {"rules": []}
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        config.setdefault("rules", [])
        return config
    except Exception as exc:  # noqa: BLE001
        log(f"WARNING: could not parse {CONFIG_FILE}, treating as no rules: {exc}")
        return {"rules": []}


# Nicknames that expand to the real substring to search for in a shift's
# title, so you can type something easier than the real title text.
# Matching checks both the typed keyword AND its alias (if any), so this
# is purely additive -- it never makes matching stricter.
TITLE_ALIASES = {
    "pac": "P&C",
}


def resolve_keyword_variants(keyword: str) -> list[str]:
    variants = [keyword]
    alias = TITLE_ALIASES.get(keyword.strip().lower())
    if alias:
        variants.append(alias)
    return variants


def shift_matches_rule(shift: dict, rule: dict) -> bool:
    title = (shift.get("title") or "").lower()
    keywords = rule.get("title_keywords")
    if keywords:
        all_variants = [v for k in keywords for v in resolve_keyword_variants(k)]
        if not any(v.lower() in title for v in all_variants):
            return False

    try:
        start_dt = datetime.strptime(shift.get("start", ""), "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(shift.get("end", ""), "%Y-%m-%d %H:%M")
    except ValueError:
        # If we can't parse the shift's own times, we can't safely evaluate
        # time/day/duration conditions -- fail closed (does not match) rather
        # than risk a false positive.
        return False

    weekday = start_dt.weekday()  # 0=Mon .. 6=Sun
    start_hm = start_dt.strftime("%H:%M")

    day_windows = rule.get("day_windows")
    if day_windows:
        # New schema: per-day time windows. A day not present in
        # day_windows is simply not allowed for this rule.
        window = day_windows.get(str(weekday))
        if not window:
            return False
        wstart, wend = window.get("start"), window.get("end")
        if wstart and wend:
            if wstart <= wend:
                if not (wstart <= start_hm <= wend):
                    return False
            else:
                # Overnight window (e.g. 22:00-06:00, like a night/"nočná"
                # shift) -- matches if the shift starts at/after wstart
                # OR at/before wend, since the window wraps past midnight.
                if not (start_hm >= wstart or start_hm <= wend):
                    return False
        elif wstart:
            if start_hm < wstart:
                return False
        elif wend:
            if start_hm > wend:
                return False
    else:
        # Legacy schema: a flat days_of_week list + one time window for all
        # of them. Kept for backwards compatibility with older config.json
        # files saved before per-day windows existed.
        days = rule.get("days_of_week")
        if days is not None and weekday not in days:
            return False
        earliest = rule.get("earliest_start_time")
        if earliest and start_hm < earliest:
            return False
        latest = rule.get("latest_start_time")
        if latest and start_hm > latest:
            return False

    duration_hours = (end_dt - start_dt).total_seconds() / 3600
    min_dur = rule.get("min_duration_hours")
    if min_dur is not None and duration_hours < min_dur:
        return False
    max_dur = rule.get("max_duration_hours")
    if max_dur is not None and duration_hours > max_dur:
        return False

    return True


def shift_matches_any_rule(shift: dict, rules: list[dict]) -> bool:
    return any(shift_matches_rule(shift, rule) for rule in rules)


def best_matching_rule_index(shift: dict, rules: list[dict]) -> int | None:
    """Rule list order IS the priority -- index 0 is highest priority.
    Returns the index of the first (highest-priority) rule this shift
    matches, or None if it matches nothing."""
    for i, rule in enumerate(rules):
        if shift_matches_rule(shift, rule):
            return i
    return None


def shifts_overlap(a: dict, b: dict) -> bool:
    try:
        a_start = datetime.strptime(a["start"], "%Y-%m-%d %H:%M")
        a_end = datetime.strptime(a["end"], "%Y-%m-%d %H:%M")
        b_start = datetime.strptime(b["start"], "%Y-%m-%d %H:%M")
        b_end = datetime.strptime(b["end"], "%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        return False
    return a_start < b_end and b_start < a_end


def claim_shift(session: requests.Session, user_id: str, shift: dict) -> bool:
    """
    Claim/sign up for a shift via smeny.cz's /shift/assign endpoint.

    Discovered from a real captured request:
      POST https://smeny.cz/shift/assign/<user_id>/<groupHash>
      (no request body -- the session cookie + groupHash is all it needs)
      Success looks like: {"messages":{"success":["...úspěšně"]}}
    """
    shift_id = shift.get("id")
    group_hash = shift.get("groupHash")
    if not group_hash:
        log(f"ERROR: shift {shift_id} has no groupHash in its data -- cannot "
            "claim it (smeny.cz may have changed its data format).")
        return False

    url = f"{BASE_URL}/shift/assign/{user_id}/{group_hash}"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": CALENDAR_URL,
        "Origin": BASE_URL,
    }

    log(f"Attempting to claim shift {shift_id}...")
    try:
        resp = session.post(url, headers=headers, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: claim request failed to send: {exc}")
        return False

    try:
        data = resp.json()
    except ValueError:
        log(f"ERROR: claim response wasn't valid JSON (status {resp.status_code}).")
        return False

    success_messages = data.get("messages", {}).get("success")
    if resp.status_code == 200 and success_messages:
        if SHOW_DETAILS_IN_LOGS:
            log(f"Claimed shift {shift_id} successfully: {success_messages}")
        else:
            log(f"Claimed shift {shift_id} successfully.")
        return True

    error_messages = data.get("messages", {}).get("error")
    if SHOW_DETAILS_IN_LOGS:
        log(f"Could not claim shift {shift_id} (status {resp.status_code}): "
            f"{error_messages or data}")
    else:
        log(f"Could not claim shift {shift_id} -- it may have already been "
            f"taken by someone else, or smeny.cz rejected the request "
            f"(status {resp.status_code}).")
    return False


def run(discover: bool = False) -> None:
    require_env("SMENY_EMAIL", "SMENY_PASSWORD")

    config = load_config()
    rules = config.get("rules", [])
    log(f"Loaded {len(rules)} auto-pick rule(s) from {CONFIG_FILE}.")

    session = login()
    user_id = get_user_id(session)
    shifts = fetch_shifts(session, user_id)

    unlocked_shifts = [s for s in shifts if s.get("unlocked")]
    available = {str(s["id"]): describe_shift(s) for s in unlocked_shifts}
    shift_by_id = {str(s["id"]): s for s in unlocked_shifts}

    # Rule list order = priority (index 0 = highest). Each shift gets the
    # index of the best (highest-priority) rule it matches, or None.
    rule_index_by_shift: dict[str, int | None] = {
        sid: best_matching_rule_index(s, rules) for sid, s in shift_by_id.items()
    }
    matched_ids = {sid for sid, idx in rule_index_by_shift.items() if idx is not None}
    log(f"Of which {len(available)} are currently unlocked/available, "
        f"{len(matched_ids)} matching your auto-pick rules.")

    if discover:
        if available:
            if SHOW_DETAILS_IN_LOGS:
                log("Available shifts found:")
                for shift_id, desc in available.items():
                    star = (
                        f" [MATCHES RULE #{rule_index_by_shift[shift_id] + 1}]"
                        if shift_id in matched_ids else ""
                    )
                    log(f"  [{shift_id}]{star} {desc}")
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

    claim_results: dict[str, bool] = {}
    skipped_conflict_ids: set[str] = set()

    if new_ids:
        log(f"{len(new_ids)} new shift(s) found!")

        # Claim in priority order (best rule first, earliest start time as
        # tiebreak), skipping anything that overlaps a shift we already
        # claimed *this run* -- you can't work two overlapping shifts, so
        # only the higher-priority one gets taken.
        to_consider = sorted(
            new_ids & matched_ids,
            key=lambda sid: (rule_index_by_shift[sid], shift_by_id[sid]["start"]),
        )
        claimed_shifts: list[dict] = []
        for shift_id in to_consider:
            candidate = shift_by_id[shift_id]
            if any(shifts_overlap(candidate, c) for c in claimed_shifts):
                skipped_conflict_ids.add(shift_id)
                log(f"Skipping shift {shift_id}: overlaps a higher-priority "
                    "shift already claimed this run.")
                continue
            success = claim_shift(session, user_id, candidate)
            claim_results[shift_id] = success
            if success:
                claimed_shifts.append(candidate)

        lines = []
        for shift_id in new_ids:
            if shift_id in skipped_conflict_ids:
                prefix = "⏭️ PRESKOČENÉ (prekrýva sa s vyššou prioritou): "
            elif shift_id in claim_results:
                prefix = (
                    "⭐ AUTOMATICKY PRIHLÁSENÉ: " if claim_results[shift_id]
                    else "⚠️ ZHODA S PRAVIDLAMI, PRIHLÁSENIE ZLYHALO (over si to ručne): "
                )
            else:
                prefix = ""
            lines.append(prefix + available[shift_id])

        message = "🟢 Nová volná smena na smeny.cz:\n\n" + "\n\n".join(lines)
        if len(message) > 1500:
            message = message[:1500] + "\n... (skrátené)"
        send_whatsapp(message)
    else:
        log("No new shifts since last check.")

    save_state(current_ids)


def test_whatsapp() -> None:
    """Send a canned message to confirm CallMeBot config works, without
    touching smeny.cz at all."""
    if not CALLMEBOT_PHONE or not CALLMEBOT_APIKEY:
        log("ERROR: CALLMEBOT_PHONE and/or CALLMEBOT_APIKEY are not set. "
            "Add them as repo secrets (or in your local .env) before testing.")
        sys.exit(1)

    log(f"Sending a test WhatsApp message via CallMeBot...")
    send_whatsapp(
        "✅ Test message from your smeny.cz shift watcher. "
        "If you got this, CallMeBot is working correctly."
    )
    log("Done. Check your WhatsApp -- if nothing arrived within a minute or "
        "two, double check CALLMEBOT_PHONE (needs the country code, e.g. "
        "+421...) and CALLMEBOT_APIKEY, and that you completed the "
        "'I allow callmebot to send me messages' step.")


if __name__ == "__main__":
    if "--test-whatsapp" in sys.argv:
        test_whatsapp()
    else:
        run(discover="--discover" in sys.argv)
