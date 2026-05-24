# Options Position Tracker

On-demand sync from Robinhood to a Google Sheet. Run `./sync` from this
directory after every trade and the **Positions** tab is rewritten with
your current portfolio:

- Account summary header (equity, today's change, buying power, cash)
- Every open option position with live mark price and unrealized P/L

Sheet1 is **not** touched — it's your hand-curated history view. The
Positions tab is the live dashboard.

## Files

- `sync_positions.py` — main script
- `sync` — convenience wrapper (`./sync` from this dir)
- `requirements.txt` — Python deps
- `.env.example` — copy to `.env` and fill in
- `service-account.json` — Google service-account key (gitignored)

## One-time setup

### 1. Google service account
1. https://console.cloud.google.com → new project → enable Google Sheets API
2. IAM & Admin → Service Accounts → create one → Keys → add JSON key
3. Save JSON as `service-account.json` next to `sync_positions.py`
4. Share your Google Sheet with the service account's `client_email` as Editor

### 2. Find your Robinhood account number
If you use Robinhood's "multiple investing accounts" feature, the API
defaults to the wrong account. Find the right one in the app:
**Account icon → Menu → Account No. → Show numbers**, copy the number
matching the account you want (e.g. "Bhuvan").

### 3. Install + configure
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD, ROBINHOOD_ACCOUNT_NUMBER,
#           SHEET_ID, GOOGLE_APPLICATION_CREDENTIALS
```

## Running

```bash
./sync
```

First run prompts for 2FA (Robinhood will text you a code, OR push to
your phone for device approval). After that the session is cached in
`~/.tokens/robinhood.pickle` and subsequent runs go straight through.

Typical runtime: ~10 seconds for ~20 positions.
