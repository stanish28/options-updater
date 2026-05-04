"""Daily options price updater.

Reads open option contracts from a Config tab in a Google Sheet, fetches the
latest last-trade price for each from Yahoo Finance via yfinance, and writes
the price x 100 back into column F of the main tab. Also writes today's date
to F6.

Configuration is via environment variables (see .env.example).
"""
from __future__ import annotations

import os
import sys
import logging

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import yfinance as yf
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


def sheets_service() -> "googleapiclient.discovery.Resource":
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


def fetch_quote(c: "Contract") -> Optional[dict]:
    """Return a dict with last/bid/ask for the contract, or None on failure."""
    try:
        ticker = yf.Ticker(c.underlying)
        chain = ticker.option_chain(c.expiration)
    except Exception as e:
        log.error("yfinance failed for %s %s: %s", c.underlying, c.expiration, e)
        return None
    df = chain.calls if c.type == "C" else chain.puts
    match = df[df["strike"] == c.strike]
    if match.empty:
        log.warning("no option row found for %s %s %s%s",
                    c.underlying, c.expiration, c.type, c.strike)
        return None
    row = match.iloc[0]
    return {
        "last": float(row.get("lastPrice")) if row.get("lastPrice") is not None else None,
        "bid":  float(row.get("bid"))       if row.get("bid")       is not None else None,
        "ask":  float(row.get("ask"))       if row.get("ask")       is not None else None,
    }


def pick_price(quote: dict) -> Optional[float]:
    last = quote.get("last")
    if last is not None and last > 0:
        return float(last)
    bid = quote.get("bid")
    ask = quote.get("ask")
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (float(bid) + float(ask)) / 2.0
    return None


def main() -> int:
    sheet_id   = env("SHEET_ID", required=True)
    main_tab   = env("MAIN_TAB", "Sheet1")
    config_tab = env("CONFIG_TAB", "Config")
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
    for c in contracts:
        label = f"{c.underlying} {c.expiration} {c.type}{c.strike}"
        quote = fetch_quote(c)
        if not quote:
            log.warning("skipping row %d (%s): no quote", c.main_row, label)
            continue
        price = pick_price(quote)
        if price is None:
            log.warning("skipping row %d (%s): no usable price (last/bid/ask all empty)", c.main_row, label)
            continue
        per_contract = round(price * 100)
        log.info("row %2d  %-30s  last=%s bid=%s ask=%s  -> $%d",
                 c.main_row, label, quote.get("last"), quote.get("bid"), quote.get("ask"), per_contract)
        updates.append({"range": f"{main_tab}!F{c.main_row}", "values": [[per_contract]]})

    today_label = datetime.now(ET).strftime("%b %-d")  # e.g. "May 5"
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
