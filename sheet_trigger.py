"""Google Sheet trigger for the options sync.

Polls a control cell on the Sheet and, when a new sync request appears, runs
./sync and writes a status line back. This lets you trigger a refresh straight
from the Sheet (via the Apps Script menu in apps_script/Sync.gs) without SSH,
the Telegram bot, or any inbound port — the VM only ever *reads/writes* the
Sheet using the service account that already has Editor access, mirroring the
outbound-only model of telegram_bot.py.

Runs as a systemd service on the VM (see options-sync-trigger.service.example),
alongside (or instead of) the Telegram bot and the cron schedule.

Control protocol (all on a dedicated `Sync` tab, created if missing):
  A1  request token  — Apps Script writes a unique token (a timestamp) to ask
                       for a sync. The poller treats any token it hasn't seen
                       before as a fresh request.
  A2  status         — the poller writes "running…/✅ done/❌ failed" + time here.

Reads from .env (same file as the rest of the project):
  SHEET_ID                       - target spreadsheet (required)
  GOOGLE_APPLICATION_CREDENTIALS - service-account JSON (required)
  SHEET_TRIGGER_POLL_SECONDS     - poll interval, default 30
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sheet-trigger")

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CONTROL_TAB = "Sync"
REQUEST_CELL = f"{CONTROL_TAB}!A1"
STATUS_CELL = f"{CONTROL_TAB}!A2"


def env(name: str, required: bool = False, default: str = "") -> str:
    val = os.environ.get(name, default)
    if required and not val:
        log.error("missing env var: %s", name)
        sys.exit(2)
    return val


def sheets_service():
    creds = Credentials.from_service_account_file(
        env("GOOGLE_APPLICATION_CREDENTIALS", required=True), scopes=SHEETS_SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def ensure_tab(svc, sheet_id: str, tab: str) -> None:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    if any(s["properties"]["title"] == tab for s in meta["sheets"]):
        return
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={
        "requests": [{"addSheet": {"properties": {"title": tab}}}],
    }).execute()
    log.info("Created missing tab: %s", tab)


def read_cell(svc, sheet_id: str, a1: str) -> str:
    res = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=a1).execute()
    vals = res.get("values", [])
    if vals and vals[0]:
        return str(vals[0][0]).strip()
    return ""


def write_cell(svc, sheet_id: str, a1: str, value: str) -> None:
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=a1,
        valueInputOption="USER_ENTERED", body={"values": [[value]]},
    ).execute()


def _now_str() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%b %-d, %Y %-I:%M %p %Z")


def run_sync() -> tuple[bool, str]:
    """Run ./sync and return (ok, short_summary). Mirrors telegram_bot.run_sync."""
    try:
        p = subprocess.run(
            ["./sync"], cwd=str(PROJECT_DIR),
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "sync timed out after 180s"
    except Exception as e:
        return False, f"failed to launch sync: {e}"
    out = (p.stdout or "") + (p.stderr or "")
    interesting = ("returned", "Wrote Positions", "Summary!", "Error",
                   "Traceback", "Authentication", "MFA")
    lines = [ln for ln in out.splitlines() if any(k in ln for k in interesting)]
    summary = lines[-1] if lines else "(no output)"
    return p.returncode == 0, summary


def main() -> int:
    sheet_id = env("SHEET_ID", required=True)
    poll_seconds = int(env("SHEET_TRIGGER_POLL_SECONDS", default="30") or "30")

    svc = sheets_service()
    ensure_tab(svc, sheet_id, CONTROL_TAB)

    # Seed last_token with whatever is already in the cell so we don't fire a
    # sync for a stale request left over from a previous run.
    last_token = read_cell(svc, sheet_id, REQUEST_CELL)
    log.info("Sheet trigger watching %s every %ds (seeded token=%r)",
             REQUEST_CELL, poll_seconds, last_token)

    while True:
        try:
            token = read_cell(svc, sheet_id, REQUEST_CELL)
            if token and token != last_token:
                last_token = token
                log.info("New sync request (token=%r) — running sync", token)
                write_cell(svc, sheet_id, STATUS_CELL, f"⏳ Running… ({_now_str()})")
                ok, summary = run_sync()
                status = "✅ Synced" if ok else "❌ Sync failed"
                write_cell(svc, sheet_id, STATUS_CELL, f"{status} {_now_str()} — {summary}")
                log.info("%s — %s", status, summary)
        except Exception as e:
            log.warning("poll error: %s", e)
        time.sleep(poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
