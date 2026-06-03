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
SUMMARY_TAB = "Summary"
SUMMARY_CASH_CELL = "H2"          # surgical write — does not touch other cells on the Summary tab


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


def fetch_account_balances(account_number: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    """Return (buying_power, cash_in_hand) in dollars from the account profile,
    or (None, None) if the API call fails. Includes unsettled funds and pending instant deposits."""
    try:
        ap = r.profiles.load_account_profile(account_number=account_number)
    except Exception as e:
        log.warning("load_account_profile failed: %s", e)
        return None, None
    if not ap:
        return None, None
    buying_power = _as_float(ap.get("buying_power"))
    cash = _as_float(ap.get("cash")) or 0.0
    unsettled = _as_float(ap.get("unsettled_funds")) or 0.0
    
    # cash in hand = settled cash (includes collateral + buying power) + unsettled funds
    cash_in_hand = cash + unsettled
    return buying_power, cash_in_hand



def fetch_positions(account_number: Optional[str]) -> list[dict]:
    raw = r.options.get_open_option_positions(account_number=account_number)
    log.info("Robinhood returned %d open option position(s) for account %s",
             len(raw), account_number or "(default)")
    positions = []
    for p in raw:
        qty = _as_float(p.get("quantity")) or 0.0
        # Contracts that expired today are still returned with their full
        # quantity until Robinhood settles the expiration overnight, but are
        # flagged via pending_expiration_quantity. Subtract those so expired
        # positions drop off the sheet immediately instead of lingering.
        qty -= _as_float(p.get("pending_expiration_quantity")) or 0.0
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


def fetch_stock_positions(account_number: Optional[str]) -> list[dict]:
    """Return open stock/share positions: list of dicts with symbol, quantity
    (shares), avg_cost (per share) and current (per share). Current prices are
    fetched in a single batched API call."""
    try:
        raw = r.account.get_open_stock_positions(account_number=account_number)
    except Exception as e:
        log.warning("get_open_stock_positions failed: %s", e)
        return []
    out: list[dict] = []
    for s in raw or []:
        qty = _as_float(s.get("quantity")) or 0.0
        if qty == 0:
            continue
        inst = r.stocks.get_instrument_by_url(s["instrument"])
        out.append({
            "symbol": inst.get("symbol", "?"),
            "quantity": qty,
            "avg_cost": _as_float(s.get("average_buy_price")) or 0.0,
            "current": None,
        })
    if out:
        try:
            prices = r.stocks.get_latest_price([d["symbol"] for d in out])
            for d, pr in zip(out, prices):
                d["current"] = _as_float(pr)
        except Exception as e:
            log.warning("stock price fetch failed: %s", e)
    log.info("Robinhood returned %d open stock position(s)", len(out))
    return out


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
ALLOCATION_FMT       = '0.0%'


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


_TABLE_COLS = 10  # columns A–J


def _range(sid: int, r0: int, r1: int, c0: int = 0, c1: int = _TABLE_COLS) -> dict:
    return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": c0, "endColumnIndex": c1}


def _align_center_req(sid: int, r0: int, r1: int) -> dict:
    return {"repeatCell": {
        "range": _range(sid, r0, r1),
        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER",
                                       "verticalAlignment": "MIDDLE"}},
        "fields": "userEnteredFormat.horizontalAlignment,userEnteredFormat.verticalAlignment",
    }}


def _header_fill_req(sid: int, row: int) -> dict:
    return {"repeatCell": {
        "range": _range(sid, row, row + 1),
        "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.85, "green": 0.89, "blue": 0.95}}},
        "fields": "userEnteredFormat.backgroundColor",
    }}


def _border_grid_req(sid: int, r0: int, r1: int) -> dict:
    line = {"style": "SOLID", "color": {"red": 0.7, "green": 0.7, "blue": 0.7}}
    return {"updateBorders": {
        "range": _range(sid, r0, r1),
        "top": line, "bottom": line, "left": line, "right": line,
        "innerHorizontal": line, "innerVertical": line,
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


def write_summary_metric(svc, sheet_id: str, cell: str, val: Optional[float], metric_name: str) -> None:
    """Surgically write a numeric value into a specific cell on the Summary tab
    and format it as currency with no decimals (e.g. $33,306). Does not touch
    any other cells on the tab."""
    if val is None:
        log.warning("Skipping Summary!%s write (%s unavailable)", cell, metric_name)
        return
    
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{SUMMARY_TAB}!{cell}",
        valueInputOption="USER_ENTERED",
        body={"values": [[round(val)]]},
    ).execute()
    
    # Extract row and column index from cell coordinate (e.g. "G2" -> row 1, col 6)
    col_str = "".join([c for c in cell if c.isalpha()])
    row_str = "".join([c for c in cell if c.isdigit()])
    
    col_idx = 0
    for char in col_str:
        col_idx = col_idx * 26 + (ord(char.upper()) - ord('A') + 1)
    col_idx -= 1 # 0-indexed
    
    row_idx = int(row_str) - 1 # 0-indexed
    
    sid = _sheet_id_int(svc, sheet_id, SUMMARY_TAB)
    
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
        "repeatCell": {
            "range": {"sheetId": sid, 
                      "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                      "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0'}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    }]}).execute()
    log.info("Wrote Summary!%s %s: $%s", cell, metric_name, f"{round(val):,}")


def build_sheet(positions: list[dict], stocks: list[dict]) -> tuple[list[list], dict]:
    """Build the Positions tab content. Returns (rows, meta) where meta carries
    the row ranges main() needs to apply number formats and bolding.

    Options are sorted by expiry (ascending) then ticker, followed by a TOTAL
    row. If there are open stock positions, a STOCKS section + STOCK TOTAL row
    is appended below the options."""
    rows: list[list] = []

    # --- Row 1: last-synced timestamp ---
    now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    rows.append([f"Last synced: {now_pt.strftime('%b %-d, %Y %-I:%M %p %Z')}"])

    # --- Row 2: header; row 3+: options body ---
    rows.append(["Ticker", "Strike", "Expiry", "Put/Call",
                 "Avg Buying Price", "No of Cons", "Current Price", "Profit/Loss",
                 "% Change", "Allocation"])

    # Allocation denominator: total current market value across all positions
    # (options + stocks). Each row's allocation = its value / this total, so they
    # sum to ~100%. Short options use the absolute buyback value as their value.
    def _opt_value(p: dict) -> float:
        c = p.get("current")
        return abs(c) * abs(p["quantity"]) if c is not None else 0.0

    def _stk_value(s: dict) -> float:
        c = s.get("current")
        return c * s["quantity"] if c is not None else 0.0

    total_value = (sum(_opt_value(p) for p in positions)
                   + sum(_stk_value(s) for s in stocks))

    opt_body_start = len(rows)
    opt_value_sum = 0.0
    total_pl = 0.0
    total_cost_basis = 0.0
    have_any_pl = False
    for p in sorted(positions, key=lambda p: (p["expiration"], p["underlying"])):
        avg_unsigned = abs(p["avg_price"])
        qty = p["quantity"]
        qty_signed = int(qty) if qty.is_integer() else qty
        current = p.get("current")
        if current is not None:
            pl = (current - avg_unsigned) * abs(qty) if qty > 0 else (avg_unsigned - current) * abs(qty)
            cost_basis = avg_unsigned * abs(qty)
            pct_change = pl / cost_basis if cost_basis > 0 else ""
            value = abs(current) * abs(qty)
            alloc_disp = value / total_value if total_value > 0 else ""
            current_disp, pl_disp, pct_disp = round(current, 2), round(pl, 2), pct_change
            total_pl += pl
            total_cost_basis += cost_basis
            opt_value_sum += value
            have_any_pl = True
        else:
            current_disp = pl_disp = pct_disp = alloc_disp = ""
        rows.append([
            p["underlying"], p["strike"], p["expiration"], p["type"],
            round(avg_unsigned, 2), qty_signed, current_disp, pl_disp, pct_disp, alloc_disp,
        ])
    opt_body_end = len(rows)

    # --- Options TOTAL row (blank spacer + TOTAL) ---
    rows.append([])
    opt_total_idx = len(rows)
    if have_any_pl:
        total_pct = total_pl / total_cost_basis if total_cost_basis > 0 else ""
        opt_alloc = opt_value_sum / total_value if total_value > 0 else ""
        rows.append(["TOTAL", "", "", "", "", "", "", round(total_pl, 2), total_pct, opt_alloc])
    else:
        rows.append(["TOTAL", "", "", "", "", "", "", "", "", ""])

    meta: dict = {
        "opt_body": (opt_body_start, opt_body_end),
        "opt_total": opt_total_idx,
        "stock_body": None,
        "stock_total": None,
        "bold_rows": [1, opt_total_idx],
    }

    # --- STOCKS section (only if there are open share positions) ---
    if stocks:
        rows.append([])
        stock_header_idx = len(rows)
        rows.append(["STOCKS", "", "", "", "Avg Cost", "Shares",
                     "Current Price", "Profit/Loss", "% Change", "Allocation"])
        stock_body_start = len(rows)
        s_total_pl = 0.0
        s_total_cost = 0.0
        s_value_sum = 0.0
        s_have = False
        for s in sorted(stocks, key=lambda x: x["symbol"]):
            avg = s["avg_cost"]
            qty = s["quantity"]
            qty_disp = int(qty) if float(qty).is_integer() else round(qty, 4)
            current = s.get("current")
            if current is not None:
                pl = (current - avg) * qty
                cost = avg * qty
                pct = pl / cost if cost > 0 else ""
                value = current * qty
                alloc_disp = value / total_value if total_value > 0 else ""
                current_disp, pl_disp, pct_disp = round(current, 2), round(pl, 2), pct
                s_total_pl += pl
                s_total_cost += cost
                s_value_sum += value
                s_have = True
            else:
                current_disp = pl_disp = pct_disp = alloc_disp = ""
            rows.append([s["symbol"], "", "", "", round(avg, 2), qty_disp,
                         current_disp, pl_disp, pct_disp, alloc_disp])
        stock_body_end = len(rows)

        rows.append([])
        stock_total_idx = len(rows)
        if s_have:
            s_total_pct = s_total_pl / s_total_cost if s_total_cost > 0 else ""
            s_alloc = s_value_sum / total_value if total_value > 0 else ""
            rows.append(["STOCK TOTAL", "", "", "", "", "", "", round(s_total_pl, 2), s_total_pct, s_alloc])
        else:
            rows.append(["STOCK TOTAL", "", "", "", "", "", "", "", "", ""])

        meta["stock_body"] = (stock_body_start, stock_body_end)
        meta["stock_total"] = stock_total_idx
        meta["bold_rows"].extend([stock_header_idx, stock_total_idx])

    return rows, meta


def main() -> int:
    sheet_id = env("SHEET_ID", required=True)
    account_number = env("ROBINHOOD_ACCOUNT_NUMBER") or None

    login()
    positions = fetch_positions(account_number)
    stocks = fetch_stock_positions(account_number)
    buying_power, cash = fetch_account_balances(account_number)

    svc = sheets_service()
    ensure_tab(svc, sheet_id, POSITIONS_TAB)
    ensure_tab(svc, sheet_id, SUMMARY_TAB)
    sheet_rows, meta = build_sheet(positions, stocks)
    sid = _sheet_id_int(svc, sheet_id, POSITIONS_TAB)

    # Number formats: E avg + G current as 2-decimals, H P/L signed currency,
    # I % change signed percent — applied to each body block and total row.
    def body_fmts(start, end):
        return [
            _num_format_req(sid, start, end, 4, NUMBER_2DEC_FMT),     # E
            _num_format_req(sid, start, end, 6, NUMBER_2DEC_FMT),     # G
            _num_format_req(sid, start, end, 7, SIGNED_CURRENCY_FMT), # H
            _num_format_req(sid, start, end, 8, SIGNED_PCT_FMT),      # I
            _num_format_req(sid, start, end, 9, ALLOCATION_FMT),      # J allocation
        ]

    def total_fmts(idx):
        return [
            _num_format_req(sid, idx, idx + 1, 7, SIGNED_CURRENCY_FMT),  # H
            _num_format_req(sid, idx, idx + 1, 8, SIGNED_PCT_FMT),       # I
            _num_format_req(sid, idx, idx + 1, 9, ALLOCATION_FMT),       # J allocation
        ]

    fmt_requests = body_fmts(*meta["opt_body"]) + total_fmts(meta["opt_total"])
    if meta["stock_body"]:
        fmt_requests += body_fmts(*meta["stock_body"]) + total_fmts(meta["stock_total"])

    # Italicize the timestamp row (idx 0); bold headers + total rows.
    fmt_requests.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"italic": True, "foregroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}}},
        "fields": "userEnteredFormat.textFormat.italic,userEnteredFormat.textFormat.foregroundColor",
    }})
    for row_idx in meta["bold_rows"]:
        fmt_requests.append(_bold_row_req(sid, row_idx))

    # Centered text across the table, header fills, and grid borders on each block.
    last_row = meta["stock_total"] or meta["opt_total"]
    fmt_requests.append(_align_center_req(sid, 1, last_row + 1))
    fmt_requests.append(_header_fill_req(sid, 1))                          # options header
    fmt_requests.append(_border_grid_req(sid, 1, meta["opt_body"][1]))     # options header + body
    fmt_requests.append(_border_grid_req(sid, meta["opt_total"], meta["opt_total"] + 1))
    if meta["stock_body"]:
        stock_header = meta["stock_body"][0] - 1
        fmt_requests.append(_header_fill_req(sid, stock_header))
        fmt_requests.append(_border_grid_req(sid, stock_header, meta["stock_body"][1]))
        fmt_requests.append(_border_grid_req(sid, meta["stock_total"], meta["stock_total"] + 1))

    write_tab(svc, sheet_id, POSITIONS_TAB, sheet_rows, format_requests=fmt_requests)
    log.info("Wrote Positions tab: %d option(s), %d stock(s) + totals", len(positions), len(stocks))

    # Surgically write cash in hand into Summary tab
    write_summary_metric(svc, sheet_id, SUMMARY_CASH_CELL, cash, "cash in hand")

    # The Summary tab mirrors the Positions table via an array formula
    # (Summary!A8 = ={Positions!A3:I}), which copies values but NOT formatting.
    # So we clear the Summary table region's formatting and paste the Positions
    # formatting onto it. Alignment offset is +5 rows: Positions header (row
    # index 1) maps to the Summary header (row index 6 / sheet row 7).
    SUMMARY_TABLE_TOP = 6  # 0-indexed; Summary row 7 = header
    summary_sid = _sheet_id_int(svc, sheet_id, SUMMARY_TAB)
    src_top, src_bottom = 1, last_row + 1   # Positions: header through last total row
    n_rows = src_bottom - src_top
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [
        # Clear stale formatting on the Summary table region (formats only —
        # leaves the mirror formulas/values intact).
        {"updateCells": {
            "range": {"sheetId": summary_sid, "startRowIndex": SUMMARY_TABLE_TOP,
                      "endRowIndex": 200, "startColumnIndex": 0, "endColumnIndex": _TABLE_COLS},
            "fields": "userEnteredFormat",
        }},
        # Copy Positions formatting (borders, fills, bold, centering, number
        # formats) onto the aligned Summary region.
        {"copyPaste": {
            "source": {"sheetId": sid, "startRowIndex": src_top, "endRowIndex": src_bottom,
                       "startColumnIndex": 0, "endColumnIndex": _TABLE_COLS},
            "destination": {"sheetId": summary_sid, "startRowIndex": SUMMARY_TABLE_TOP,
                            "endRowIndex": SUMMARY_TABLE_TOP + n_rows,
                            "startColumnIndex": 0, "endColumnIndex": _TABLE_COLS},
            "pasteType": "PASTE_FORMAT",
        }},
    ]}).execute()
    log.info("Mirrored Positions formatting onto Summary tab")

    return 0


if __name__ == "__main__":
    sys.exit(main())
