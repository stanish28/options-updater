# Options Position Tracker

Pulls live option positions from Robinhood and mirrors them to a Google Sheet's `Positions` tab. Run `./sync` on demand after a trade, or set up the included LaunchAgent (macOS) / cron job (Linux) to refresh every weekday morning automatically.

The sheet ends up looking like:

| Ticker | Strike | Expiry | Put/Call | Avg Buying Price | No of Cons | Current Price | Profit/Loss | % Change |
|---|---|---|---|---|---|---|---|---|
| BBAI | 4 | 2026-05-29 | P | 18.00 | -2 | 1.50 | +$33.00 | +91.67% |
| ... | | | | | | | | |
| **TOTAL** | | | | | | | **-$3,201.00** | **-21.37%** |

With a `Last synced: ...` timestamp at the top, positions sorted by expiry ascending.

## Prerequisites

- macOS or Linux (Windows untested; should work in WSL)
- **Python 3.9+** — needed for `zoneinfo` (stdlib timezone support)
- A Google account
- A Robinhood account with open option positions

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/stanish28/options-updater.git
cd options-updater
```

### 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Google service account (for writing to the Sheet)

1. Open https://console.cloud.google.com → create a new project (any name, e.g. `options-updater`)
2. Search for "Google Sheets API" in the top search bar → **Enable**
3. Sidebar → **IAM & Admin → Service Accounts → Create service account**
   - Name: anything, e.g. `sheets-writer`
   - Skip role grants → **Done**
4. Click the new service account → **Keys** tab → **Add Key → Create new key → JSON**
   - A `.json` file downloads. Move it next to `sync_positions.py` (or anywhere; remember the path)
5. Open the JSON, copy the `client_email` value (looks like `sheets-writer@<project>.iam.gserviceaccount.com`)
6. Open your Google Sheet → **Share** → paste that email → role **Editor** → Share

### 4. Find your Robinhood account number (only if you have multiple accounts under one login)

1. Open the Robinhood app → tap your **profile icon** (bottom right)
2. Tap **Menu** → **Account No. → Show numbers**
3. Copy the number for the account you want to track (e.g. the "Bhuvan" or "Joint" tab)

If you only have one Robinhood account, skip this step — leave `ROBINHOOD_ACCOUNT_NUMBER` blank in `.env`.

### 5. Create your `.env`

```bash
cp .env.example .env
```

Edit `.env` and fill in real values:

```
SHEET_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz...      # from your sheet URL
GOOGLE_APPLICATION_CREDENTIALS=/Users/you/options-updater/service-account.json
ROBINHOOD_USERNAME=you@example.com
ROBINHOOD_PASSWORD=your_password
ROBINHOOD_ACCOUNT_NUMBER=                     # blank if single account
```

⚠ Both `.env` and `*.json` are in `.gitignore` — they will **not** be committed.

### 6. First run

```bash
./sync
```

First time only, Robinhood will require 2FA:

- **Push notification** → tap "Approve" on your phone
- **SMS code** → script prompts `Please type in the MFA code:` — type the 6-digit code Robinhood texted you, press Enter

The session is cached in `~/.tokens/robinhood.pickle`, so subsequent runs skip 2FA for a few weeks until the session expires.

Expected output:
```
INFO Logged in
INFO Robinhood returned 18 open option position(s) for account 504794249
INFO Wrote Positions tab: 18 position(s) + totals
```

Open your Google Sheet — the `Positions` tab should now be populated.

## Automated daily refresh (optional)

### macOS — LaunchAgent + pmset wake

A `com.options-sync.plist.example` template is in this repo. To set up:

```bash
# 1. Copy + customize the plist
cp com.options-sync.plist.example ~/Library/LaunchAgents/com.options-sync.plist
# Open it in your editor and replace /Users/YOURNAME with your actual home path

# 2. Load it
launchctl load -w ~/Library/LaunchAgents/com.options-sync.plist

# 3. (Optional) Schedule macOS to wake the Mac at the same time so launchd can fire
#    even when the laptop is asleep. Requires the Mac to be plugged in.
sudo pmset repeat wakeorpoweron MTWRF 05:55:00

# 4. Verify
launchctl list | grep options-sync     # should show the label, last exit 0
pmset -g sched                          # should show "wakepoweron at 5:55AM weekdays only"
```

Now the sync fires at 6 AM PT every weekday automatically. Logs to `launchd.log` in the project dir.

To disable: `launchctl unload ~/Library/LaunchAgents/com.options-sync.plist` and `sudo pmset repeat cancel`.

### Linux — cron

```bash
# Edit your crontab
crontab -e

# Add this line — fires every weekday at 6 AM:
0 6 * * 1-5 cd /path/to/options-updater && ./sync >> launchd.log 2>&1
```

The machine must be on at the scheduled time. Cron does not wake sleeping hardware.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Operation not permitted` from launchd | macOS TCC blocks Desktop/Documents folders — keep the project in `~/` not `~/Desktop/` |
| Robinhood `Authentication required` on every run | Session expired — run `./sync` from terminal once, complete MFA, cached session resumes |
| Strike column shows dates like `1900-01-03` | Old write left a stale format. Script auto-resets format each run — just run `./sync` again |
| Positions count seems wrong | Check `ROBINHOOD_ACCOUNT_NUMBER` — multi-account logins default to one account unless you specify |
| `Last synced` shows the wrong time | Timezone defaults to your machine's local time. If running headless server, set the OS timezone or change `ZoneInfo("America/Los_Angeles")` in `sync_positions.py` |

## Files in this repo

- `sync_positions.py` — main script
- `sync` — convenience wrapper (`./sync` from project dir)
- `requirements.txt` — Python dependencies
- `.env.example` — template for credentials/config
- `com.options-sync.plist.example` — macOS LaunchAgent template
- `.gitignore` — excludes `.env`, service-account JSON, venv, logs

## Security notes

- `.env` contains your Robinhood password in plaintext on disk. The default `chmod` of files in your home directory should keep it readable only by your user, but consider extra protection if your machine is shared.
- `service-account.json` is a private key. Treat it like a password — never commit, never paste in screenshots.
- This script is local-only by design. Robinhood credentials are NEVER sent to any cloud service.
