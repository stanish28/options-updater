# Options Price Auto-Updater

Daily job that updates column F ("Current Price per Con") of the options
tracking Google Sheet with the latest last-trade price for each open contract,
plus the as-of date in F6. Designed to run unattended at 4:30 PM ET on
weekdays. Quote source: Yahoo Finance (via `yfinance`) — no API key required.

## Files
- `update_prices.py` — main script
- `requirements.txt` — Python deps
- `.env.example` — copy to `.env` and fill in
- `service_account.json` — Google service-account key (you provide; gitignored)

## One-time setup

### 1. Google service account
1. https://console.cloud.google.com → new project `options-updater`.
2. Enable the Google Sheets API.
3. IAM & Admin → Service Accounts → create `sheets-writer` → Keys → add JSON key.
4. Download the JSON, save as `service_account.json` next to `update_prices.py`.
5. Copy the `client_email` from the JSON, share the Google Sheet with that email as Editor.

### 2. Config tab
The script's setup step creates a `Config` tab with header `Underlying | Expiration | Strike | Type | MainSheetRow`. Fill one row per open contract:
- `Expiration` in `YYYY-MM-DD`
- `Type` is `C` (call) or `P` (put)
- `MainSheetRow` is the row number on the main tab whose F-cell to overwrite

Update this tab whenever you open or close a contract.

### 3. Local install
```
cd ~/Desktop/options_updater
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Running

Dry run (no sheet writes):
```
DRY_RUN=1 python update_prices.py
```

Update one row only:
```
ONLY_ROW=16 python update_prices.py
```

Full live run:
```
python update_prices.py
```

Force-run on a market-closed day (testing):
```
SKIP_HOLIDAY_CHECK=1 python update_prices.py
```

## Schedule
Production schedule: cron `30 16 * * 1-5` in `America/New_York` (4:30 PM ET, weekdays). Script self-skips on NYSE holidays.
