"""Read latest_prices.json and write its prices to Sheet1.

Runs inside the Claude routine after the repo is cloned with the latest
price file already committed by the GitHub Action.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("apply")

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
INPUT_PATH = "latest_prices.json"
TIMESTAMP_CELL = "F6"
MAX_AGE_HOURS = 25  # reject data older than this so we don't write stale prices


def env(name: str, required: bool = False, default: str = "") -> str:
    val = os.environ.get(name, default)
    if required and not val:
        log.error("missing env var: %s", name)
        sys.exit(2)
    return val


def main() -> int:
    sheet_id = env("SHEET_ID", required=True)
    main_tab = env("MAIN_TAB", default="Sheet1")
    creds_path = env("GOOGLE_APPLICATION_CREDENTIALS", required=True)

    if not os.path.exists(INPUT_PATH):
        log.error("%s not found in cwd; did the GitHub Action run today?", INPUT_PATH)
        return 1

    with open(INPUT_PATH) as f:
        payload = json.load(f)

    asof_iso = payload.get("asof_iso", "")
    asof_label = payload.get("asof_label", "")
    prices = payload.get("prices") or []

    if not prices:
        log.error("%s has no prices; aborting", INPUT_PATH)
        return 1

    try:
        asof_dt = datetime.fromisoformat(asof_iso)
        if asof_dt.tzinfo is None:
            asof_dt = asof_dt.replace(tzinfo=timezone.utc)
        age = datetime.now(asof_dt.tzinfo) - asof_dt
        if age > timedelta(hours=MAX_AGE_HOURS):
            log.error("data is stale (%.1f hours old, max %d); aborting", age.total_seconds() / 3600, MAX_AGE_HOURS)
            return 1
        log.info("data age: %.1f hours", age.total_seconds() / 3600)
    except (ValueError, TypeError) as e:
        log.error("could not parse asof_iso=%r: %s; aborting", asof_iso, e)
        return 1

    creds = Credentials.from_service_account_file(creds_path, scopes=SHEETS_SCOPES)
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    updates = [{"range": f"{main_tab}!F{p['row']}", "values": [[p["price"]]]} for p in prices]
    updates.append({"range": f"{main_tab}!{TIMESTAMP_CELL}", "values": [[asof_label]]})

    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    log.info("wrote %d cell(s) to sheet (asof %s)", len(updates), asof_label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
