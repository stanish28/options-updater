"""On-demand Robinhood → Google Sheet sync.

Run from the Mac terminal whenever you want a fresh view of your portfolio.
Logs into Robinhood (prompts for SMS/MFA the first time; session is cached
in ~/.tokens/robinhood.pickle so subsequent runs go straight through),
pulls the account summary + open option positions from the configured
account, and writes everything to the **Positions** tab of the Google
Sheet.

Sheet1 is untouched — that stays your hand-curated history. The Positions
tab is the live view, rewritten end-to-end on every run.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

import robin_stocks.robinhood as r
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync")

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
POSITIONS_TAB = "Positions"


def env(name: str, required: bool = False, default: str = "") -> str:
    val = os.environ.get(name, default)
    if required and not val:
        log.error("missing env var: %s", name)
        sys.exit(2)
    return val


def login() -> None:
    user = env("ROBINHOOD_USERNAME", required=True)
    pwd = env("ROBINHOOD_PASSWORD", required=True)
    log.info("Logging into Robinhood... (may prompt for 2FA on first run)")
    r.login(user, pwd)
    log.info("Logged in")


def _as_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_account_summary(account_number: Optional[str]) -> dict:
    """Return account-level dashboard numbers: equity, day change, buying power, cash."""
    port = r.profiles.load_portfolio_profile(account_number=account_number) or {}
    acct = r.profiles.load_account_profile(account_number=account_number) or {}
    equity = _as_float(port.get("equity"))
    prev_close_equity = _as_float(port.get("adjusted_equity_previous_close"))
    day_change = None
    day_change_pct = None
    if equity is not None and prev_close_equity not in (None, 0):
        day_change = equity - prev_close_equity
        day_change_pct = (day_change / prev_close_equity) * 100
    return {
        "equity": equity,
        "day_change": day_change,
        "day_change_pct": day_change_pct,
        "buying_power": _as_float(acct.get("buying_power")),
        "cash": _as_float(acct.get("cash")),
    }


def fetch_market_price(option_id: str) -> Optional[float]:
    """Return current mark price per CONTRACT (mark × 100) for an option, or None."""
    try:
        mkt = r.options.get_option_market_data_by_id(option_id)
    except Exception as e:
        log.warning("market_data fetch failed for %s: %s", option_id, e)
        return None
    if not mkt:
        return None
    m = mkt[0] if isinstance(mkt, list) else mkt
    for key in ("mark_price", "adjusted_mark_price", "last_trade_price"):
        v = m.get(key)
        if v not in (None, "", "0", "0.0", "0.00", "0.000000"):
            try:
                return float(v) * 100
            except (ValueError, TypeError):
                pass
    bid, ask = m.get("bid_price"), m.get("ask_price")
    if bid and ask:
        try:
            return ((float(bid) + float(ask)) / 2.0) * 100
        except (ValueError, TypeError):
            pass
    return None


def fetch_positions(account_number: Optional[str]) -> list[dict]:
    raw = r.options.get_open_option_positions(account_number=account_number)
    log.info("Robinhood returned %d open option position(s) for account %s",
             len(raw), account_number or "(default)")
    positions = []
    for p in raw:
        qty = _as_float(p.get("quantity")) or 0.0
        if qty == 0:
            continue
        instrument_url = p.get("option")
        if not instrument_url:
            continue
        details = r.helper.request_get(instrument_url)
        if not details:
            continue
        option_id = instrument_url.rstrip("/").split("/")[-1]
        side_sign = -1 if p.get("type") == "short" else 1
        positions.append({
            "underlying": details["chain_symbol"],
            "expiration": details["expiration_date"],
            "strike": float(details["strike_price"]),
            "type": details["type"][0].upper(),
            "quantity": qty * side_sign,
            "avg_price": float(p.get("average_price") or 0),
            "current": fetch_market_price(option_id),
        })
    return positions


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


CURRENCY_FMT = '"$"#,##0.00'
SIGNED_CURRENCY_FMT = '[Color10]"+$"#,##0.00;[Red]"-$"#,##0.00;"$"0.00'
SIGNED_PCT_FMT       = '[Color10]"+"0.00%;[Red]"-"0.00%;0.00%'


def _sheet_id_int(svc, sheet_id: str, tab: str) -> int:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return next(s["properties"]["sheetId"] for s in meta["sheets"]
                if s["properties"]["title"] == tab)


def write_tab(svc, sheet_id: str, tab: str, values: list[list],
              summary_formats: Optional[list[Optional[str]]] = None) -> None:
    """Reset formatting+values on the tab then write fresh. If summary_formats
    is given, apply those number formats to row 2 (the dashboard values row)."""
    sid = _sheet_id_int(svc, sheet_id, tab)
    requests = [{
        "updateCells": {
            "range": {"sheetId": sid},
            "fields": "userEnteredFormat,userEnteredValue",
        }
    }]
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{tab}!A1",
        valueInputOption="USER_ENTERED", body={"values": values},
    ).execute()
    if summary_formats:
        fmt_requests = []
        for col, pattern in enumerate(summary_formats):
            if not pattern:
                continue
            fmt_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 1, "endRowIndex": 2,        # row 2 (zero-indexed)
                        "startColumnIndex": col, "endColumnIndex": col + 1,
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            })
        # Bold the two header rows
        for row in (0, 3):
            fmt_requests.append({
                "repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": row, "endRowIndex": row + 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            })
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": fmt_requests}).execute()


def build_sheet(summary: dict, positions: list[dict]) -> list[list]:
    """Build the full Positions tab content: summary header + position table.
    Summary values are raw numbers; display formatting is applied separately."""
    rows: list[list] = []

    # --- Account dashboard (rows 1-2) ---
    rows.append(["Account Value", "Today's Change", "Today's %", "Buying Power", "Cash"])
    rows.append([
        summary["equity"] if summary["equity"] is not None else "",
        summary["day_change"] if summary["day_change"] is not None else "",
        (summary["day_change_pct"] / 100) if summary["day_change_pct"] is not None else "",
        summary["buying_power"] if summary["buying_power"] is not None else "",
        summary["cash"] if summary["cash"] is not None else "",
    ])

    # --- Blank spacer (row 3) ---
    rows.append([])

    # --- Position table (rows 4 and on) ---
    rows.append(["Ticker", "Strike", "Expiry", "Put/Call",
                 "Avg Buying Price", "No of Cons", "Current Price", "Profit/Loss"])
    for p in positions:
        avg_unsigned = abs(p["avg_price"])
        qty = p["quantity"]
        qty_signed = int(qty) if qty.is_integer() else qty
        current = p.get("current")
        if current is not None:
            if qty > 0:
                pl = (current - avg_unsigned) * abs(qty)
            else:
                pl = (avg_unsigned - current) * abs(qty)
            current_disp = round(current)
            pl_disp = round(pl)
        else:
            current_disp = ""
            pl_disp = ""
        rows.append([
            p["underlying"], p["strike"], p["expiration"], p["type"],
            round(avg_unsigned, 2), qty_signed, current_disp, pl_disp,
        ])
    return rows


def main() -> int:
    sheet_id = env("SHEET_ID", required=True)
    account_number = env("ROBINHOOD_ACCOUNT_NUMBER") or None

    login()
    summary = fetch_account_summary(account_number)
    positions = fetch_positions(account_number)

    log.info("Account: equity=$%s day_change=$%s buying_power=$%s",
             f"{summary['equity']:,.2f}" if summary['equity'] else "?",
             f"{summary['day_change']:+,.2f}" if summary['day_change'] is not None else "?",
             f"{summary['buying_power']:,.2f}" if summary['buying_power'] else "?")

    svc = sheets_service()
    ensure_tab(svc, sheet_id, POSITIONS_TAB)
    summary_formats = [CURRENCY_FMT, SIGNED_CURRENCY_FMT, SIGNED_PCT_FMT, CURRENCY_FMT, CURRENCY_FMT]
    write_tab(svc, sheet_id, POSITIONS_TAB,
              build_sheet(summary, positions), summary_formats=summary_formats)
    log.info("Wrote Positions tab: %d position(s) + summary header", len(positions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
