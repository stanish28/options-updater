"""Fetch option prices from Polygon and write latest_prices.json.

Runs inside the GitHub Action (which has unrestricted egress). Reads the
Config tab from the same Google Sheet, queries Polygon /prev for each
contract, and writes a JSON file the Claude routine then applies to Sheet1.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fetch")

ET = ZoneInfo("America/New_York")
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
POLYGON_BASE = "https://api.polygon.io"
POLYGON_RATE_DELAY_S = 13
OUT_PATH = "latest_prices.json"


@dataclass
class Contract:
    underlying: str
    expiration: str
    strike: float
    type: str
    main_row: int

    def occ(self) -> str:
        y, m, d = self.expiration.split("-")
        return f"O:{self.underlying.upper()}{y[-2:]}{m}{d}{self.type}{int(round(self.strike * 1000)):08d}"


def env(name: str, required: bool = False) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        log.error("missing env var: %s", name)
        sys.exit(2)
    return val


def read_config(sheets, sheet_id: str, config_tab: str) -> list[Contract]:
    rng = f"{config_tab}!A2:E"
    resp = sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    out: list[Contract] = []
    for i, row in enumerate(resp.get("values", []), start=2):
        if not row or all(not str(c).strip() for c in row):
            continue
        if len(row) < 5:
            log.warning("Config row %d invalid: %r", i, row)
            continue
        try:
            out.append(Contract(row[0].strip(), row[1].strip(), float(row[2]), row[3].strip().upper(), int(row[4])))
        except (ValueError, IndexError) as e:
            log.warning("Config row %d parse error (%s): %r", i, e, row)
    return out


def fetch_close(occ: str, api_key: str) -> Optional[float]:
    url = f"{POLYGON_BASE}/v2/aggs/ticker/{occ}/prev"
    for attempt in range(3):
        try:
            r = requests.get(url, params={"apikey": api_key, "adjusted": "true"}, timeout=30)
        except requests.RequestException as e:
            if attempt < 2:
                log.warning("connection error %s (attempt %d): %s; retrying in 30s", occ, attempt + 1, e)
                time.sleep(30)
                continue
            log.warning("connection error %s after retries: %s", occ, e)
            return None
        if r.status_code == 200:
            results = r.json().get("results") or []
            if results and results[0].get("c") is not None:
                return float(results[0]["c"])
            log.warning("polygon /prev %s -> 200 but no results", occ)
            return None
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            log.warning("polygon %d on %s; sleeping 60s", r.status_code, occ)
            time.sleep(60)
            continue
        log.warning("polygon /prev %s -> %d %s", occ, r.status_code, r.text[:200])
        return None
    return None


def main() -> int:
    sheet_id = env("SHEET_ID", required=True)
    config_tab = env("CONFIG_TAB") or "Config"
    polygon_key = env("POLYGON_API_KEY", required=True)
    creds_path = env("GOOGLE_APPLICATION_CREDENTIALS", required=True)

    creds = Credentials.from_service_account_file(creds_path, scopes=SHEETS_SCOPES)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    contracts = read_config(sheets, sheet_id, config_tab)
    log.info("loaded %d contract(s)", len(contracts))

    prices: list[dict] = []
    for i, c in enumerate(contracts):
        occ = c.occ()
        close = fetch_close(occ, polygon_key)
        if close is None:
            log.warning("row %d %s: no Polygon data", c.main_row, occ)
            if i < len(contracts) - 1:
                time.sleep(POLYGON_RATE_DELAY_S)
            continue
        per_contract = round(close * 100)
        log.info("row %2d  %-30s  close=%.2f -> $%d", c.main_row, occ, close, per_contract)
        prices.append({"row": c.main_row, "price": per_contract})
        if i < len(contracts) - 1:
            time.sleep(POLYGON_RATE_DELAY_S)

    if not prices:
        log.error("zero contracts succeeded; not writing JSON")
        return 1

    now_et = datetime.now(ET)
    payload = {
        "asof_iso": now_et.isoformat(),
        "asof_label": now_et.strftime("%b %-d"),
        "prices": prices,
        "fetched_count": len(prices),
        "expected_count": len(contracts),
    }
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    log.info("wrote %s with %d/%d prices, asof=%s", OUT_PATH, len(prices), len(contracts), payload["asof_label"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
