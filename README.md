# smeny.cz shift watcher → WhatsApp

Checks your smeny.cz account every 10 minutes for newly-available shifts
(the ones you can pickup, shown with an unlocked-padlock indicator / a
green calendar date in the app) and WhatsApps you when one shows up.
Runs for free on GitHub Actions, so your own computer doesn't need to be on.

## How it works

smeny.cz's own website loads your shifts from a JSON API
(`/shift/user-list/<your-id>`), where each shift has an `"unlocked": true/false`
field. `watch_shifts.py` logs in the same way the website does (a plain
HTTP request, no browser needed), calls that same API for the next ~60
days, and checks which shifts are unlocked. It compares that list to the
last run (`state.json`) and WhatsApps you about anything new, via
[CallMeBot](https://www.callmebot.com/) (free).

A GitHub Actions workflow runs this on a schedule, 24/7, for free.

## One-time setup

### 1. Get a CallMeBot API key

1. Save `+34 644 59 71 67` as a contact in WhatsApp (call it "CallMeBot").
2. Message it exactly: `I allow callmebot to send me messages`
3. Within a minute or two it replies with your personal API key.

### 2. Put the code on GitHub

1. Create a **private** GitHub repository (private matters — this repo
   will reference your smeny.cz login via secrets).
2. Push/upload these files to it, keeping the folder structure (in
   particular `.github/workflows/*.yml` needs to stay under that exact path).

### 3. Add your secrets

In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add each of:

| Secret name        | Value                                      |
| ------------------- | ------------------------------------------ |
| `SMENY_EMAIL`       | your smeny.cz login email                  |
| `SMENY_PASSWORD`    | your smeny.cz password                     |
| `CALLMEBOT_PHONE`   | your WhatsApp number, e.g. `+421900000000` |
| `CALLMEBOT_APIKEY`  | the key CallMeBot sent you                 |

### 4. Test it

Go to the **Actions** tab → **"Discover shift page (test run, no WhatsApp
sent)"** → **Run workflow**. This logs in and reports what it finds,
without sending any WhatsApp message or saving any state, so it's safe to
run as many times as you like. Click into the run and open the "Run
discovery" step to read the log — it'll say how many shifts it found and
how many are currently available.

If it fails, open the failing step and read the error — it'll usually
tell you directly what's wrong (e.g. wrong password, or smeny.cz changed
something on their end). Paste it back to me if you're not sure what it
means.

### 5. Turn on the real thing

Once discovery looks right, the **"Check smeny.cz shifts"** workflow is
already scheduled to run automatically every 10 minutes — you don't need
to do anything else. You can also trigger it manually from the Actions
tab the same way, to test that a real WhatsApp message arrives.

## Adjusting the check frequency

Edit the `cron` line in `.github/workflows/check-shifts.yml`. It's currently
`*/10 * * * *` (every 10 minutes). GitHub's minimum practical interval is
about 5 minutes, and free-tier scheduled runs can occasionally be delayed
under load.

## Adjusting the lookahead window

By default the script checks the next 60 days. To change this, add a
repository secret (or just edit the default in `watch_shifts.py`) called
`LOOKAHEAD_DAYS`.

## Notes / limits

- CallMeBot is a free hobby service maintained by a third party, not an
  official WhatsApp Business integration — fine for personal alerts like
  this, but occasionally rate-limited.
- Keep the GitHub repo **private** since it references your login via
  secrets (secrets themselves aren't visible in logs, but treat the repo
  as sensitive regardless).
- If smeny.cz changes its login page or API, the script may need updating
  — re-run the discovery workflow and check the error message, or send it
  to me.
