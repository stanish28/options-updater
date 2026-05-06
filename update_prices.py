"""Daily options price updater.

Reads open option contracts from a Config tab in a Google Sheet, fetches each
contract's most-recent close price from Polygon.io, and writes the price x 100
back into column F of the main tab. Also writes today's date to F6.

Configuration is via environment variables (see .env.example).
"""
from __future__ import annotations

import os
import sys
import time
import logging

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import pandas_market_calendars as mcal

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("options_updater")

ET = ZoneInfo("America/New_York")
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TIMESTAMP_CELL = "F6"
POLYGON_BASE = "https://api.polygon.io"
POLYGON_RATE_DELAY_S = 13  # free tier: 5 req/min, leave headroom


@dataclass
class Contract:
    underlying: str
    expiration: str  # YYYY-MM-DD
    strike: float
    type: str        # "C" or "P"
    main_row: int


def env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        log.error("missing required env var: %s", name)
        sys.exit(2)
    return val or ""


def market_open_today(skip: bool) -> bool:
    if skip:
        return True
    today = datetime.now(ET).date()
    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=today, end_date=today)
    return not sched.empty


def sheets_service():
    creds_path = env("GOOGLE_APPLICATION_CREDENTIALS", required=True)
    creds = Credentials.from_service_account_file(creds_path, scopes=SHEETS_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_config(sheets, sheet_id: str, config_tab: str) -> list[Contract]:
    rng = f"{config_tab}!A2:E"
    resp = sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    rows = resp.get("values", [])
    contracts: list[Contract] = []
    for i, row in enumerate(rows, start=2):
        if not row or all(not str(c).strip() for c in row):
            continue
        if len(row) < 5:
            log.warning("Config row %d has fewer than 5 columns, skipping: %r", i, row)
            continue
        try:
            contracts.append(
                Contract(
                    underlying=row[0].strip(),
                    expiration=row[1].strip(),
                    strike=float(row[2]),
                    type=row[3].strip().upper(),
                    main_row=int(row[4]),
                )
            )
        except (ValueError, IndexError) as e:
            log.warning("Config row %d invalid (%s): %r", i, e, row)
    return contracts


def occ_ticker(c: Contract) -> str:
    """Build Polygon's options ticker, e.g. O:NNE260515P00019000."""
    y, m, d = c.expiration.split("-")
    yymmdd = f"{y[-2:]}{m}{d}"
    strike_int = int(round(c.strike * 1000))
    return f"O:{c.underlying.upper()}{yymmdd}{c.type.upper()}{strike_int:08d}"


def fetch_close(occ: str, api_key: str) -> Optional[float]:
    """Fetch the most-recent close from Polygon /prev. After EOD processing
    (~5-6 PM ET) /prev returns today's close. Free tier supports this endpoint.
    Retries once on 429."""
    url = f"{POLYGON_BASE}/v2/aggs/ticker/{occ}/prev"
    for attempt in range(2):
        try:
            r = requests.get(url, params={"apikey": api_key, "adjusted": "true"}, timeout=20)
        except requests.RequestException as e:
            log.warning("polygon request failed for %s: %s", occ, e)
            return None
        if r.status_code == 200:
            results = r.json().get("results") or []
            if results and results[0].get("c") is not None:
                return float(results[0]["c"])
            log.warning("polygon /prev %s -> 200 but no results", occ)
            return None
        if r.status_code == 429 and attempt == 0:
            log.warning("polygon 429 on %s; sleeping 60s and retrying once", occ)
            time.sleep(60)
            continue
        log.warning("polygon /prev %s -> %d %s", occ, r.status_code, r.text[:200])
        return None
    return None


def main() -> int:
    sheet_id   = env("SHEET_ID", required=True)
    main_tab   = env("MAIN_TAB", "Sheet1")
    config_tab = env("CONFIG_TAB", "Config")
    api_key    = env("POLYGON_API_KEY", required=True)
    dry_run    = env("DRY_RUN") == "1"
    only_row   = int(env("ONLY_ROW")) if env("ONLY_ROW") else None
    skip_hol   = env("SKIP_HOLIDAY_CHECK") == "1"

    if not market_open_today(skip_hol):
        log.info("NYSE closed today, exiting cleanly.")
        return 0

    sheets = sheets_service()
    contracts = read_config(sheets, sheet_id, config_tab)
    if only_row is not None:
        contracts = [c for c in contracts if c.main_row == only_row]
    log.info("loaded %d contract(s) from %s", len(contracts), config_tab)
    if not contracts:
        log.warning("no contracts to update")
        return 0

    updates: list[dict] = []
    for i, c in enumerate(contracts):
        label = f"{c.underlying} {c.expiration} {c.type}{c.strike}"
        occ = occ_ticker(c)
        close = fetch_close(occ, api_key)
        if close is None:
            log.warning("skipping row %d (%s, %s): no Polygon data", c.main_row, label, occ)
        else:
            per_contract = round(close * 100)
            log.info("row %2d  %-30s  %s  close=%.2f -> $%d",
                     c.main_row, label, occ, close, per_contract)
            updates.append({"range": f"{main_tab}!F{c.main_row}", "values": [[per_contract]]})

        # Rate-limit pause between contracts (skip after last)
        if i < len(contracts) - 1:
            time.sleep(POLYGON_RATE_DELAY_S)

    if not updates:
        log.error("no prices fetched for any contract; skipping timestamp write so sheet doesn't appear fresh")
        return 1

    today_label = datetime.now(ET).strftime("%b %-d")
    updates.append({"range": f"{main_tab}!{TIMESTAMP_CELL}", "values": [[today_label]]})

    if dry_run:
        log.info("DRY_RUN=1 -> would batchUpdate %d ranges:", len(updates))
        for u in updates:
            log.info("  %s = %s", u["range"], u["values"][0][0])
        return 0

    body = {"valueInputOption": "USER_ENTERED", "data": updates}
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
    log.info("wrote %d cell(s) to sheet", len(updates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
