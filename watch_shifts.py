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

import email
import email.utils
import imaplib
import json
import os
import re
import sys
import time
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

# smeny.cz added 2FA: a 6-digit code emailed to you, required on every
# login. To keep this fully automated, the script reads that code straight
# out of your inbox via IMAP right after triggering login.
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
# TODO: confirm these two against a real 2FA email and tighten them --
# using the sender address (not just a hint) and the exact subject line
# makes the inbox search far less likely to ever grab the wrong email.
TWO_FA_SENDER_HINT = os.environ.get("TWO_FA_SENDER_HINT", "info@smeny.cz")
TWO_FA_SUBJECT_HINT = os.environ.get("TWO_FA_SUBJECT_HINT", "Ověřovací kód pro přihlášení")

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
TWO_FA_POST_URL = f"{BASE_URL}/2fa_check"
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


def _extract_email_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="ignore"))
        return "\n".join(parts)
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")


def _try_fetch_2fa_code(after_ts: float) -> str | None:
    """Look for the newest email received after after_ts that looks like
    the 2FA code email, and pull a 6-digit code out of it. Returns None
    (not an error) if nothing matching has arrived yet -- the caller polls."""
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX")

        criteria = ["FROM", f'"{TWO_FA_SENDER_HINT}"']
        if TWO_FA_SUBJECT_HINT:
            criteria += ["SUBJECT", f'"{TWO_FA_SUBJECT_HINT}"']

        # The subject has Czech diacritics (Ověřovací...), which plain
        # US-ASCII IMAP search (imaplib's default) can't match -- encode
        # as UTF-8 and tell the server explicitly when any criterion has
        # non-ASCII characters.
        if any(not c.isascii() for c in criteria):
            encoded = [c.encode("utf-8") if not c.isascii() else c for c in criteria]
            status, data = imap.search("UTF-8", *encoded)
        else:
            status, data = imap.search(None, *criteria)
        if status != "OK" or not data or not data[0]:
            return None

        # Check the newest few messages, most recent first.
        msg_ids = data[0].split()[-10:][::-1]
        for msg_id in msg_ids:
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])

            date_header = msg.get("Date")
            if not date_header:
                continue
            try:
                msg_ts = email.utils.parsedate_to_datetime(date_header).timestamp()
            except (TypeError, ValueError):
                continue
            # 30s buffer for clock skew between smeny.cz's mail server and
            # wherever this script is running.
            if msg_ts < after_ts - 30:
                continue

            body = _extract_email_text(msg)
            match = re.search(r"\b(\d{6})\b", body)
            if match:
                return match.group(1)
        return None
    finally:
        imap.logout()


def fetch_2fa_code(after_ts: float, timeout_seconds: int = 90, poll_interval: int = 5) -> str:
    """Poll the inbox for up to timeout_seconds for a 2FA code email sent
    after after_ts (the moment we submitted the login form)."""
    require_env("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
    deadline = time.time() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        code = _try_fetch_2fa_code(after_ts)
        if code:
            log(f"Found 2FA code in inbox (attempt {attempt}).")
            return code
        if time.time() >= deadline:
            log(f"ERROR: no 2FA code email found within {timeout_seconds}s. "
                "Check TWO_FA_SENDER_HINT/TWO_FA_SUBJECT_HINT match the "
                "real email, and that GMAIL_ADDRESS/GMAIL_APP_PASSWORD are "
                "correct.")
            sys.exit(1)
        log(f"No 2FA code yet (attempt {attempt}), waiting {poll_interval}s...")
        time.sleep(poll_interval)


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

    login_attempt_ts = time.time()
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

    if resp.url.rstrip("/").endswith("/2fa"):
        log("2FA required -- waiting for the verification code email...")
        code_match = CSRF_RE.search(resp.text)
        if not code_match:
            log("ERROR: could not find _csrf_token on the /2fa page. "
                "smeny.cz may have changed this form -- inspect it manually.")
            sys.exit(1)
        two_fa_csrf_token = code_match.group(1)

        auth_code = fetch_2fa_code(after_ts=login_attempt_ts)

        log("Submitting 2FA code")
        resp = session.post(
            TWO_FA_POST_URL,
            data={
                "_auth_code": auth_code,
                "_csrf_token": two_fa_csrf_token,
            },
            timeout=30,
        )
        resp.raise_for_status()

        if resp.url.rstrip("/").endswith("/2fa"):
            log("ERROR: still on the /2fa page after submitting the code -- "
                "it was likely wrong, expired, or already used. Check "
                "TWO_FA_SENDER_HINT/TWO_FA_SUBJECT_HINT aren't matching a "
                "stale email.")
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


def resolve_date_range_bounds(date_range: dict | None) -> tuple[date, date] | None:
    """Turns a rule's date_range into a concrete (start_date, end_date), or
    None if there's no restriction. "this_week"/"next_week" are resolved
    relative to *today* (whenever this actually runs), not baked in at
    config-save time, so they stay correct as calendar weeks roll over.
    Weeks run Monday-Sunday, matching smeny.cz's own convention."""
    if not date_range:
        return None
    rtype = date_range.get("type", "none")
    if rtype == "none":
        return None
    if rtype == "this_week":
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)
    if rtype == "next_week":
        today = date.today()
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
        return monday, monday + timedelta(days=6)
    if rtype == "custom":
        try:
            start = datetime.strptime(date_range["start"], "%Y-%m-%d").date()
            end = datetime.strptime(date_range["end"], "%Y-%m-%d").date()
            return start, end
        except (KeyError, ValueError):
            return None
    return None


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

    date_bounds = resolve_date_range_bounds(rule.get("date_range"))
    if date_bounds:
        range_start, range_end = date_bounds
        if not (range_start <= start_dt.date() <= range_end):
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
                if not (wstart <= start_hm < wend):
                    return False
            else:
                # Overnight window (e.g. 22:00-06:00, like a night/"nočná"
                # shift) -- matches if the shift starts at/after wstart
                # OR strictly before wend, since the window wraps past
                # midnight. End is exclusive so a shift starting exactly
                # at wend (e.g. 06:00) belongs to the *next* window
                # (e.g. "ranná" starting 06:00), not this one.
                if not (start_hm >= wstart or start_hm < wend):
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
    max_shifts_per_day = config.get("max_shifts_per_day", 1)
    log(f"Loaded {len(rules)} auto-pick rule(s) from {CONFIG_FILE} "
        f"(max {max_shifts_per_day} auto-claimed shift(s)/day).")

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
    skipped_daylimit_ids: set[str] = set()

    if new_ids:
        log(f"{len(new_ids)} new shift(s) found!")

        # Claim in priority order (best rule first, earliest start time as
        # tiebreak), skipping anything that overlaps a shift we already
        # claimed *this run* -- you can't work two overlapping shifts, so
        # only the higher-priority one gets taken. Also enforce
        # max_shifts_per_day even for non-overlapping shifts on the same
        # calendar day (e.g. a ranná + poobedná combo), since claiming both
        # is technically possible but likely not what you want.
        to_consider = sorted(
            new_ids & matched_ids,
            key=lambda sid: (rule_index_by_shift[sid], shift_by_id[sid]["start"]),
        )
        claimed_shifts: list[dict] = []
        claimed_count_by_date: dict[str, int] = {}
        for shift_id in to_consider:
            candidate = shift_by_id[shift_id]
            if any(shifts_overlap(candidate, c) for c in claimed_shifts):
                skipped_conflict_ids.add(shift_id)
                log(f"Skipping shift {shift_id}: overlaps a higher-priority "
                    "shift already claimed this run.")
                continue

            candidate_date = candidate["start"].split(" ")[0]
            if claimed_count_by_date.get(candidate_date, 0) >= max_shifts_per_day:
                skipped_daylimit_ids.add(shift_id)
                log(f"Skipping shift {shift_id}: already claimed "
                    f"{max_shifts_per_day} shift(s) on {candidate_date} this run "
                    "(max_shifts_per_day limit).")
                continue

            success = claim_shift(session, user_id, candidate)
            claim_results[shift_id] = success
            if success:
                claimed_shifts.append(candidate)
                claimed_count_by_date[candidate_date] = claimed_count_by_date.get(candidate_date, 0) + 1

        lines = []
        for shift_id in new_ids:
            if shift_id in skipped_conflict_ids:
                prefix = "⏭️ PRESKOČENÉ (prekrýva sa s vyššou prioritou): "
            elif shift_id in skipped_daylimit_ids:
                prefix = "⏭️ PRESKOČENÉ (dosiahnutý denný limit smien): "
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
