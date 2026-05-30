"""On-demand Robinhood → Google Sheet sync.

Run from the Mac terminal whenever you want a fresh view of your portfolio.
Logs into Robinhood (prompts for SMS/MFA the first time; session is cached
in ~/.tokens/robinhood.pickle so subsequent runs go straight through),
pulls the open option positions from the configured account, and writes
them to the **Positions** tab of the Google Sheet.

Sheet1 is untouched — that stays your hand-curated history. The Positions
tab is the live view, rewritten end-to-end on every run.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

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


NUMBER_2DEC_FMT = '#,##0.00'
SIGNED_CURRENCY_FMT = '[Color10]"+$"#,##0.00;[Red]"-$"#,##0.00;"$"0.00'
SIGNED_PCT_FMT       = '[Color10]"+"0.00%;[Red]"-"0.00%;0.00%'


def _sheet_id_int(svc, sheet_id: str, tab: str) -> int:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    return next(s["properties"]["sheetId"] for s in meta["sheets"]
                if s["properties"]["title"] == tab)


def _num_format_req(sid: int, start_row: int, end_row: int, col: int, pattern: str) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sid,
                  "startRowIndex": start_row, "endRowIndex": end_row,
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
        "fields": "userEnteredFormat.numberFormat",
    }}


def _bold_row_req(sid: int, row: int) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": row, "endRowIndex": row + 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold",
    }}


def write_tab(svc, sheet_id: str, tab: str, values: list[list],
              format_requests: Optional[list[dict]] = None) -> None:
    """Reset formatting+values on the tab then write fresh. If format_requests is
    given, run them as a follow-up batchUpdate to apply number formats / bolding."""
    sid = _sheet_id_int(svc, sheet_id, tab)
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
        "updateCells": {
            "range": {"sheetId": sid},
            "fields": "userEnteredFormat,userEnteredValue",
        }
    }]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{tab}!A1",
        valueInputOption="USER_ENTERED", body={"values": values},
    ).execute()
    if format_requests:
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id,
                                       body={"requests": format_requests}).execute()


def build_sheet(positions: list[dict]) -> tuple[list[list], int]:
    """Build the Positions tab content. Returns (rows, total_pl_row_index).

    Positions are sorted by expiry (ascending). A TOTAL row is appended at the
    bottom summing P/L."""
    rows: list[list] = []

    # --- Row 1: last-synced timestamp ---
    now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    rows.append([f"Last synced: {now_pt.strftime('%b %-d, %Y %-I:%M %p %Z')}"])

    # --- Row 2: header; row 3+: body ---
    rows.append(["Ticker", "Strike", "Expiry", "Put/Call",
                 "Avg Buying Price", "No of Cons", "Current Price", "Profit/Loss", "% Change"])

    sorted_positions = sorted(positions, key=lambda p: p["expiration"])

    total_pl = 0.0
    total_cost_basis = 0.0
    have_any_pl = False
    for p in sorted_positions:
        avg_unsigned = abs(p["avg_price"])
        qty = p["quantity"]
        qty_signed = int(qty) if qty.is_integer() else qty
        current = p.get("current")
        if current is not None:
            if qty > 0:
                pl = (current - avg_unsigned) * abs(qty)
            else:
                pl = (avg_unsigned - current) * abs(qty)
            cost_basis = avg_unsigned * abs(qty)
            pct_change = pl / cost_basis if cost_basis > 0 else None
            current_disp = round(current, 2)
            pl_disp = round(pl, 2)
            pct_disp = pct_change if pct_change is not None else ""
            total_pl += pl
            total_cost_basis += cost_basis
            have_any_pl = True
        else:
            current_disp = ""
            pl_disp = ""
            pct_disp = ""
        rows.append([
            p["underlying"], p["strike"], p["expiration"], p["type"],
            round(avg_unsigned, 2), qty_signed, current_disp, pl_disp, pct_disp,
        ])

    # --- Totals row at the bottom (blank spacer + TOTAL row) ---
    rows.append([])
    total_row_idx = len(rows)        # 1-based position of TOTAL row when written
    if have_any_pl:
        total_pct = total_pl / total_cost_basis if total_cost_basis > 0 else ""
        rows.append(["TOTAL", "", "", "", "", "", "", round(total_pl, 2), total_pct])
    else:
        rows.append(["TOTAL", "", "", "", "", "", "", "", ""])
    return rows, total_row_idx


def main() -> int:
    sheet_id = env("SHEET_ID", required=True)
    account_number = env("ROBINHOOD_ACCOUNT_NUMBER") or None

    login()
    positions = fetch_positions(account_number)

    svc = sheets_service()
    ensure_tab(svc, sheet_id, POSITIONS_TAB)
    sheet_rows, total_row_idx = build_sheet(positions)
    sid = _sheet_id_int(svc, sheet_id, POSITIONS_TAB)

    # Layout: row 1 (idx 0) = timestamp, row 2 (idx 1) = header, rows 3+ = body
    body_start = 2                              # zero-indexed = sheet row 3 (first position)
    body_end = total_row_idx                    # exclusive end = TOTAL row index
    fmt_requests = [
        _num_format_req(sid, body_start, body_end, 4, NUMBER_2DEC_FMT),     # E avg
        _num_format_req(sid, body_start, body_end, 6, NUMBER_2DEC_FMT),     # G current
        _num_format_req(sid, body_start, body_end, 7, SIGNED_CURRENCY_FMT), # H P/L
        _num_format_req(sid, body_start, body_end, 8, SIGNED_PCT_FMT),      # I % change
        _num_format_req(sid, total_row_idx, total_row_idx + 1, 7, SIGNED_CURRENCY_FMT),  # TOTAL P/L
        _num_format_req(sid, total_row_idx, total_row_idx + 1, 8, SIGNED_PCT_FMT),       # TOTAL %
    ]
    # Italicize the timestamp row (idx 0), bold the table header (idx 1) and TOTAL row
    fmt_requests.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"italic": True, "foregroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}}},
        "fields": "userEnteredFormat.textFormat.italic,userEnteredFormat.textFormat.foregroundColor",
    }})
    for row_idx in (1, total_row_idx):
        fmt_requests.append(_bold_row_req(sid, row_idx))

    write_tab(svc, sheet_id, POSITIONS_TAB, sheet_rows, format_requests=fmt_requests)
    log.info("Wrote Positions tab: %d position(s) + totals", len(positions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
