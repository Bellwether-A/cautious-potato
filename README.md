# smeny.cz shift watcher → WhatsApp

Checks your smeny.cz account every 10 minutes for newly-available shifts
and WhatsApps you when one shows up. Runs for free on GitHub Actions, so
your own computer doesn't need to be on.

## How it works

1. A small Python script (`watch_shifts.py`) uses a headless browser
   (Playwright) to log into smeny.cz with your credentials, exactly like
   your phone would.
2. It looks at the shifts page and finds any shift marked as "unlocked" /
   available to claim.
3. It compares that list to the last run (`state.json`) and WhatsApps you
   about anything new, via [CallMeBot](https://www.callmebot.com/) (free).
4. A GitHub Actions workflow runs this on a schedule, 24/7, for free.

## One-time setup

### 1. Get a CallMeBot API key

1. Save `+34 644 59 71 67` as a contact in WhatsApp (call it "CallMeBot").
2. Message it exactly: `I allow callmebot to send me messages`
3. Within a minute or two it replies with your personal API key.

### 2. Put the code on GitHub

1. Create a **private** GitHub repository (private matters — this repo
   will reference your smeny.cz login).
2. Push these files to it.

### 3. Find the real shifts page and selectors (discovery step)

I couldn't log into your account to check the exact page structure, so
there's a one-time calibration step:

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # then fill in SMENY_EMAIL / SMENY_PASSWORD
export $(grep -v '^#' .env | xargs)
python watch_shifts.py --discover
```

This logs in and saves `discover_screenshot.png` + `discover_page.html`.
Open the screenshot, find how an available/unlocked shift looks (per
smeny.cz's own help docs, it shows an open-padlock icon), then open
`discover_page.html`, search for the matching element, and update the
`SELECTORS` dictionary near the top of `watch_shifts.py` — mainly:

- `shift_card` — the CSS selector matching each shift entry
- `unlocked_marker` — the CSS selector matching the "available" icon/badge

If you're not comfortable reading HTML, send me the screenshot and the
relevant snippet of `discover_page.html` and I'll fill in the exact
selectors for you.

**If login doesn't work automatically** (e.g. smeny.cz shows a CAPTCHA or
2FA step), the fallback is cookie-based auth: log in manually once in a
real browser, export the session cookie, and load it in the script instead
of filling the login form. Let me know if you hit this and I'll add it.

### 4. Add secrets to GitHub

In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add each of:

| Secret name        | Value                                      |
| ------------------- | ------------------------------------------ |
| `SMENY_EMAIL`       | your smeny.cz login email                  |
| `SMENY_PASSWORD`    | your smeny.cz password                     |
| `SHIFTS_URL`        | the shifts page URL you found above        |
| `CALLMEBOT_PHONE`   | your WhatsApp number, e.g. `+421900000000` |
| `CALLMEBOT_APIKEY`  | the key CallMeBot sent you                 |

### 5. Turn it on

Go to the **Actions** tab of your repo, enable workflows if prompted, and
either wait for the next scheduled run or click **Run workflow** to test
it immediately.

## Adjusting the check frequency

Edit the `cron` line in `.github/workflows/check-shifts.yml`. It's currently
`*/10 * * * *` (every 10 minutes). GitHub's minimum practical interval is
about 5 minutes, and free-tier scheduled runs can occasionally be delayed
under load.

## Notes / limits

- CallMeBot is a free hobby service maintained by a third party, not an
  official WhatsApp Business integration — fine for personal alerts like
  this, but occasionally rate-limited.
- Keep the GitHub repo **private** since it references your login via
  secrets (secrets themselves aren't visible in logs, but treat the repo
  as sensitive regardless).
- If smeny.cz changes its page layout, the selectors in `SELECTORS` may
  need re-calibrating — rerun `--discover` if the watcher suddenly stops
  detecting shifts.
