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


def build_sheet(summary: dict, positions: list[dict]) -> tuple[list[list], int]:
    """Build the full Positions tab content. Returns (rows, total_pl_row_index).

    Summary values are raw numbers; display formatting is applied separately.
    Positions are sorted by expiry (ascending). A TOTAL row is appended at the
    bottom summing P/L so you can sanity-check against the account dashboard."""
    rows: list[list] = []

    # --- Account dashboard (rows 1-2) ---
    rows.append(["Account Value", "Today's Change", "Today's %", "Buying Power", "Cash"])
    rows.append([
        summary["equity"]                                     if summary["equity"] is not None else "",
        summary["day_change"]                                 if summary["day_change"] is not None else "",
        (summary["day_change_pct"] / 100)                     if summary["day_change_pct"] is not None else "",
        summary["buying_power"]                               if summary["buying_power"] is not None else "",
        summary["cash"]                                       if summary["cash"] is not None else "",
    ])

    # --- Blank spacer (row 3) ---
    rows.append([])

    # --- Position table header (row 4) + body (rows 5+) ---
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
    summary = fetch_account_summary(account_number)
    positions = fetch_positions(account_number)

    log.info("Account: equity=$%s day_change=$%s buying_power=$%s",
             f"{summary['equity']:,.2f}" if summary['equity'] else "?",
             f"{summary['day_change']:+,.2f}" if summary['day_change'] is not None else "?",
             f"{summary['buying_power']:,.2f}" if summary['buying_power'] else "?")

    svc = sheets_service()
    ensure_tab(svc, sheet_id, POSITIONS_TAB)
    sheet_rows, total_row_idx = build_sheet(summary, positions)
    sid = _sheet_id_int(svc, sheet_id, POSITIONS_TAB)

    fmt_requests = []
    # Summary row (row 2, zero-indexed=1)
    for col, pattern in enumerate(
        [CURRENCY_FMT, SIGNED_CURRENCY_FMT, SIGNED_PCT_FMT, CURRENCY_FMT, CURRENCY_FMT]):
        fmt_requests.append(_num_format_req(sid, 1, 2, col, pattern))
    # Position-table body + TOTAL row: columns E (avg), G (current), H (P/L), I (% change)
    body_start = 4                              # zero-indexed row 4 = sheet row 5 (first position)
    body_end = total_row_idx                    # zero-indexed exclusive end = TOTAL row index
    fmt_requests += [
        _num_format_req(sid, body_start, body_end, 4, NUMBER_2DEC_FMT),     # E avg
        _num_format_req(sid, body_start, body_end, 6, NUMBER_2DEC_FMT),     # G current
        _num_format_req(sid, body_start, body_end, 7, SIGNED_CURRENCY_FMT), # H P/L
        _num_format_req(sid, body_start, body_end, 8, SIGNED_PCT_FMT),      # I % change
        _num_format_req(sid, total_row_idx, total_row_idx + 1, 7, SIGNED_CURRENCY_FMT),  # TOTAL P/L
        _num_format_req(sid, total_row_idx, total_row_idx + 1, 8, SIGNED_PCT_FMT),       # TOTAL %
    ]
    # Bold: header row 1 (idx 0), table header row 4 (idx 3), TOTAL row
    for r in (0, 3, total_row_idx):
        fmt_requests.append(_bold_row_req(sid, r))

    write_tab(svc, sheet_id, POSITIONS_TAB, sheet_rows, format_requests=fmt_requests)
    log.info("Wrote Positions tab: %d position(s) + summary + totals", len(positions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
