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
import signal
import sys
from datetime import date, datetime
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
REALIZED_TAB = "Realized P/L"   # script-owned; the hand-kept "Closed Positions" tab is NEVER touched

# --- Risk flag (Positions column M) -------------------------------------
# Surfaces LONG options heading for a worthless expiry while there's still time
# to act. Motivated by the realized-P/L review (2026-09-23): 8 contracts that
# expired worthless cost $12,038 -- 7 of them long calls that went to zero,
# several held for months at deep losses (WPM sat at -98% for weeks).
# Long-only on purpose: a SHORT option expiring worthless is a win, so flagging
# shorts would be pure noise.
FLAG_COL = 12                   # 0-indexed -> column M (K/L hold the cash block)
FLAG_DOWN_PCT = -0.75           # flag a long option down 75%+
FLAG_DAYS_LEFT = 21             # ...or within 21 days of expiry


def _risk_flag(qty: float, pct_change, expiry: Optional[str], today: date) -> str:
    """Return a short warning string for an at-risk LONG option, else ''."""
    if qty <= 0:
        return ""
    parts = []
    if isinstance(pct_change, (int, float)) and pct_change <= FLAG_DOWN_PCT:
        parts.append(f"⚠️ Down {abs(pct_change) * 100:.0f}%")
    try:
        days = (date.fromisoformat(str(expiry)) - today).days
        if 0 <= days <= FLAG_DAYS_LEFT:
            parts.append(f"⏳ {days}d left")
    except (TypeError, ValueError):
        pass
    return " · ".join(parts)
# Cash metrics have been moved to the Positions tab (column K/L).
# They are no longer written to the Summary tab directly.


def env(name: str, required: bool = False, default: str = "") -> str:
    val = os.environ.get(name, default)
    if required and not val:
        log.error("missing env var: %s", name)
        sys.exit(2)
    return val


def telegram_send(text: str) -> bool:
    """Send a Telegram message to TELEGRAM_ALERT_CHAT_ID via TELEGRAM_BOT_TOKEN.

    No-ops (returns False) if either is unset, and never raises — notifications
    must never mask or interrupt the work that triggered them."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data),
            timeout=15,
        )
        log.info("Sent Telegram message to chat %s", chat)
        return True
    except Exception as e:
        log.warning("Could not send Telegram message: %s", e)
        return False


def notify_failure(err) -> None:
    """Telegram-alert the owner when a sync fails, so silent scheduled failures
    (usually an expired Robinhood session) don't leave the sheet quietly stale."""
    msg = str(err) or type(err).__name__
    when = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%b %-d, %-I:%M %p %Z")
    session_issue = any(k in msg.lower() for k in
                        ("logged in", "login", "auth", "mfa", "challenge", "verification"))
    if session_issue:
        text = (f"⚠️ Options sync FAILED — {when}\n"
                "Robinhood session has likely expired.\n\n"
                "Fix: run ./sync on your Mac, approve the Robinhood push on your phone, "
                "then re-push the session to the VM.\n\n"
                f"Details: {msg[:300]}")
    else:
        text = f"⚠️ Options sync FAILED — {when}\n\nDetails: {msg[:400]}"
    telegram_send(text)


class LoginTimeout(Exception):
    """Raised when the Robinhood login exceeds LOGIN_TIMEOUT_SECONDS."""


def login() -> None:
    """Log into Robinhood, with a hard cap on how long we'll wait.

    Why the cap: when the cached session expires, robin_stocks falls into a
    device-approval flow whose inner poll loop (`while True: sleep(5)` in
    authentication._validate_sherrif_id) is UNBOUNDED. On an unattended cron run
    nobody ever taps "approve", so the run hangs ~50 minutes until some network
    error finally kills it — delaying the failure alert by the same ~50 minutes.
    A SIGALRM cap turns that into a fast, clean failure that alerts immediately.
    Default 120s: long enough for a present user to approve the push, short
    enough that unattended runs give up quickly. Override with
    LOGIN_TIMEOUT_SECONDS."""
    user = env("ROBINHOOD_USERNAME", required=True)
    pwd = env("ROBINHOOD_PASSWORD", required=True)
    try:
        timeout = int(env("LOGIN_TIMEOUT_SECONDS", default="120") or 120)
    except ValueError:
        timeout = 120
    log.info("Logging into Robinhood... (may prompt for 2FA; %ds cap)", timeout)

    def _on_timeout(signum, frame):
        raise LoginTimeout(
            f"Robinhood login timed out after {timeout}s waiting for device approval — "
            "the cached session has expired and no one approved the push."
        )

    # SIGALRM is Unix-only and main-thread-only; both hold for cron/bot/manual
    # runs here. Fall back to an uncapped login anywhere it isn't available.
    use_alarm = hasattr(signal, "SIGALRM")
    prev_handler = None
    if use_alarm:
        prev_handler = signal.signal(signal.SIGALRM, _on_timeout)
        signal.alarm(timeout)
    try:
        r.login(user, pwd)
    finally:
        if use_alarm:
            signal.alarm(0)
            if prev_handler is not None:
                signal.signal(signal.SIGALRM, prev_handler)
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


def fetch_account_balances(account_number: Optional[str]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (buying_power, options_collateral, portfolio_cash) in dollars from the account profile,
    or (None, None, None) if the API call fails."""
    try:
        ap = r.profiles.load_account_profile(account_number=account_number)
    except Exception as e:
        log.warning("load_account_profile failed: %s", e)
        return None, None, None
    if not ap:
        return None, None, None
    buying_power = _as_float(ap.get("buying_power"))
    options_collateral = _as_float(ap.get("cash_held_for_options_collateral"))
    portfolio_cash = _as_float(ap.get("portfolio_cash"))
    
    # Fallback to cash_balances sub-dict if top-level values are missing
    cb = ap.get("cash_balances") or {}
    if buying_power is None:
        buying_power = _as_float(cb.get("buying_power"))
    if options_collateral is None:
        options_collateral = _as_float(cb.get("cash_held_for_options_collateral"))
    if portfolio_cash is None:
        portfolio_cash = _as_float(cb.get("portfolio_cash"))
        if portfolio_cash is None:
            cash = _as_float(ap.get("cash")) or _as_float(cb.get("cash")) or 0.0
            unsettled = _as_float(ap.get("unsettled_funds")) or _as_float(cb.get("unsettled_funds")) or 0.0
            portfolio_cash = cash + unsettled
            
    return buying_power, options_collateral, portfolio_cash



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


def _align_center_req(sid: int, r0: int, r1: int, cols: int = _TABLE_COLS) -> dict:
    return {"repeatCell": {
        "range": _range(sid, r0, r1, 0, cols),
        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER",
                                       "verticalAlignment": "MIDDLE"}},
        "fields": "userEnteredFormat.horizontalAlignment,userEnteredFormat.verticalAlignment",
    }}


def _header_fill_req(sid: int, row: int, cols: int = _TABLE_COLS) -> dict:
    return {"repeatCell": {
        "range": _range(sid, row, row + 1, 0, cols),
        "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.85, "green": 0.89, "blue": 0.95}}},
        "fields": "userEnteredFormat.backgroundColor",
    }}


def _border_grid_req(sid: int, r0: int, r1: int, cols: int = _TABLE_COLS) -> dict:
    line = {"style": "SOLID", "color": {"red": 0.7, "green": 0.7, "blue": 0.7}}
    return {"updateBorders": {
        "range": _range(sid, r0, r1, 0, cols),
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


# write_summary_metric was removed since cash metrics moved to Positions tab


def build_sheet(positions: list[dict], stocks: list[dict], buying_power: Optional[float] = None, options_collateral: Optional[float] = None, portfolio_cash: Optional[float] = None, initial_invested: float = 45000.0) -> tuple[list[list], dict]:
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

    # Allocation denominator: total initial invested capital of the portfolio
    # (fetched dynamically from Summary!B1).
    def _opt_cost(p: dict) -> float:
        return abs(p["avg_price"]) * abs(p["quantity"])

    opt_cost_sum = sum(_opt_cost(p) for p in positions)

    opt_body_start = len(rows)
    total_pl = 0.0
    total_cost_basis = 0.0
    have_any_pl = False
    today = date.today()
    flag_cells: list[tuple[int, str]] = []   # (row index, flag text) -> column M
    for p in sorted(positions, key=lambda p: (p["expiration"], p["underlying"])):
        avg_unsigned = abs(p["avg_price"])
        qty = p["quantity"]
        qty_signed = int(qty) if qty.is_integer() else qty
        cost_basis = avg_unsigned * abs(qty)
        alloc_disp = cost_basis / initial_invested if initial_invested > 0 else ""
        current = p.get("current")
        if current is not None:
            pl = (current - avg_unsigned) * abs(qty) if qty > 0 else (avg_unsigned - current) * abs(qty)
            pct_change = pl / cost_basis if cost_basis > 0 else ""
            current_disp, pl_disp, pct_disp = round(current, 2), round(pl, 2), pct_change
            total_pl += pl
            total_cost_basis += cost_basis
            have_any_pl = True
        else:
            current_disp = pl_disp = pct_disp = ""
        flag = _risk_flag(qty, pct_disp, p["expiration"], today)
        if flag:
            flag_cells.append((len(rows), flag))
        rows.append([
            p["underlying"], p["strike"], p["expiration"], p["type"],
            round(avg_unsigned, 2), qty_signed, current_disp, pl_disp, pct_disp, alloc_disp,
        ])
    opt_body_end = len(rows)

    # --- Options TOTAL row (blank spacer + TOTAL) ---
    rows.append([])
    opt_total_idx = len(rows)
    opt_alloc = opt_cost_sum / initial_invested if initial_invested > 0 else ""
    if have_any_pl:
        total_pct = total_pl / total_cost_basis if total_cost_basis > 0 else ""
        rows.append(["TOTAL", "", "", "", "", "", "", round(total_pl, 2), total_pct, opt_alloc])
    else:
        rows.append(["TOTAL", "", "", "", "", "", "", "", "", opt_alloc])

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
        s_have = False
        stk_cost_sum = sum(s["avg_cost"] * s["quantity"] for s in stocks)
        for s in sorted(stocks, key=lambda x: x["symbol"]):
            avg = s["avg_cost"]
            qty = s["quantity"]
            qty_disp = int(qty) if float(qty).is_integer() else round(qty, 4)
            cost = avg * qty
            alloc_disp = cost / initial_invested if initial_invested > 0 else ""
            current = s.get("current")
            if current is not None:
                pl = (current - avg) * qty
                pct = pl / cost if cost > 0 else ""
                current_disp, pl_disp, pct_disp = round(current, 2), round(pl, 2), pct
                s_total_pl += pl
                s_total_cost += cost
                s_have = True
            else:
                current_disp = pl_disp = pct_disp = ""
            rows.append([s["symbol"], "", "", "", round(avg, 2), qty_disp,
                         current_disp, pl_disp, pct_disp, alloc_disp])
        stock_body_end = len(rows)

        rows.append([])
        stock_total_idx = len(rows)
        s_alloc = stk_cost_sum / initial_invested if initial_invested > 0 else ""
        if s_have:
            s_total_pct = s_total_pl / s_total_cost if s_total_cost > 0 else ""
            rows.append(["STOCK TOTAL", "", "", "", "", "", "", round(s_total_pl, 2), s_total_pct, s_alloc])
        else:
            rows.append(["STOCK TOTAL", "", "", "", "", "", "", "", "", s_alloc])

        meta["stock_body"] = (stock_body_start, stock_body_end)
        meta["stock_total"] = stock_total_idx
        meta["bold_rows"].extend([stock_header_idx, stock_total_idx])

    # Ensure rows has at least 4 elements so we can write to K2:L4
    while len(rows) < 4:
        rows.append([])

    # Pad rows to make sure we can write to columns K and L (indexes 10 and 11)
    # Row 2 (index 1): Cash in hand
    if portfolio_cash is not None:
        while len(rows[1]) < 10:
            rows[1].append("")
        rows[1].extend(["Cash in hand", round(portfolio_cash)])
        
    # Row 3 (index 2): Buying Power
    if buying_power is not None:
        while len(rows[2]) < 10:
            rows[2].append("")
        rows[2].extend(["Buying Power", round(buying_power)])
        
    # Row 4 (index 3): Options Collateral
    if options_collateral is not None:
        while len(rows[3]) < 10:
            rows[3].append("")
        rows[3].extend(["Options Collateral", round(options_collateral)])

    # --- Column M risk flags -------------------------------------------------
    # Applied LAST on purpose: the K/L cash block above pads rows to length 10
    # then extends, so writing column M earlier would shift those cash values.
    def _set_flag_col(row_idx: int, value: str) -> None:
        row = rows[row_idx]
        while len(row) < FLAG_COL:
            row.append("")
        if len(row) == FLAG_COL:
            row.append(value)
        else:
            row[FLAG_COL] = value

    if flag_cells:
        _set_flag_col(1, "Flag")            # header cell M2
        for idx, text in flag_cells:
            _set_flag_col(idx, text)
    meta["flag_rows"] = [idx for idx, _ in flag_cells]

    return rows, meta


def fetch_realized_trades(account_number: Optional[str]) -> list[dict]:
    """Reconstruct realized P/L for every closed option contract from order history.

    Robinhood does NOT store realized P/L — a closed position's `average_price`
    resets to 0 — so we net the cash flows per contract instead:
        credit order = money in (+),  debit order = money out (-)

    A contract counts as realized once it is no longer among the open positions.
    That deliberately also captures contracts that **expired worthless or were
    assigned**, which produce no closing order at all (the old blocker that kept
    this tab manual).

    Accuracy notes:
      * Single-leg orders use the order's `net_amount` (fees included) — this was
        validated to the dollar against hand-kept records (SPOT/CEG/PANW/ORCL/
        USO/SNOW/INTU all matched).
      * Multi-leg (spread) orders are split per leg using each leg's own
        `executions` (gross of fees), since one order premium covers both legs.
      * History only goes back as far as Robinhood's order feed (~Feb 2026);
        older trades cannot be reconstructed.
      * Only this account — other sub-accounts are not reachable via the API.
    """
    try:
        raw_open = r.options.get_open_option_positions(account_number=account_number) or []
        orders = r.orders.get_all_option_orders(account_number=account_number) or []
    except Exception as e:
        log.warning("realized P/L fetch failed: %s", e)
        return []

    open_ids = {p["option"].rstrip("/").split("/")[-1]
                for p in raw_open if (_as_float(p.get("quantity")) or 0) != 0}

    acc: dict[str, dict] = {}
    for o in orders:
        if o.get("state") != "filled":
            continue
        legs = o.get("legs") or []
        multi = len(legs) > 1
        date = (o.get("created_at") or "")[:10]
        for lg in legs:
            oid = lg["option"].rstrip("/").split("/")[-1]
            c = acc.setdefault(oid, {"cash": 0.0, "open_q": 0.0, "close_q": 0.0, "symbol": None,
                                     "strike": 0.0, "type": "?", "expiry": None,
                                     "opened": None, "closed": None})
            if multi:
                execs = lg.get("executions") or []
                value = sum((_as_float(e.get("price")) or 0) * (_as_float(e.get("quantity")) or 0) * 100
                            for e in execs)
                c["cash"] += value if lg.get("side") == "sell" else -value
                qty = sum(_as_float(e.get("quantity")) or 0 for e in execs)
            else:
                sign = 1.0 if o.get("direction") == "credit" else -1.0
                c["cash"] += sign * (_as_float(o.get("net_amount")) or 0)
                qty = _as_float(o.get("processed_quantity")) or 0
            if lg.get("position_effect") == "open":
                c["open_q"] += qty
            else:
                c["close_q"] += qty
            c["symbol"] = o.get("chain_symbol")
            c["strike"] = _as_float(lg.get("strike_price")) or 0.0
            c["type"] = (lg.get("option_type") or "?")[0].upper()
            c["expiry"] = lg.get("expiration_date")
            c["opened"] = date if not c["opened"] else min(c["opened"], date)
            c["closed"] = date if not c["closed"] else max(c["closed"], date)

    trades = []
    for oid, c in acc.items():
        if oid in open_ids or c["open_q"] <= 0:
            continue  # still open, or we only ever saw a closing leg
        c["outcome"] = "Closed" if c["close_q"] >= c["open_q"] - 1e-9 else "Expired/Assigned"
        c["contracts"] = int(c["open_q"]) if float(c["open_q"]).is_integer() else c["open_q"]
        trades.append(c)
    trades.sort(key=lambda t: (t["closed"] or "", t["symbol"] or ""), reverse=True)
    log.info("Reconstructed %d realized option trade(s)", len(trades))
    return trades


def write_realized_tab(svc, sheet_id: str, trades: list[dict]) -> None:
    """Rewrite the script-owned Realized P/L tab (newest trade first + TOTAL)."""
    rows: list[list] = []
    now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    rows.append([f"Last synced: {now_pt.strftime('%b %-d, %Y %-I:%M %p %Z')}"])
    rows.append(["Ticker", "Strike", "Expiry", "Put/Call", "Contracts",
                 "Opened", "Closed", "Realized P/L", "Outcome"])
    body_start = len(rows)
    total = 0.0
    for t in trades:
        total += t["cash"]
        rows.append([t["symbol"], t["strike"], t["expiry"], t["type"], t["contracts"],
                     t["opened"], t["closed"], round(t["cash"], 2), t["outcome"]])
    body_end = len(rows)
    rows.append([])
    total_idx = len(rows)
    rows.append(["TOTAL", "", "", "", "", "", "", round(total, 2), ""])

    cols = 9
    sid = _sheet_id_int(svc, sheet_id, REALIZED_TAB)
    fmt = [
        _num_format_req(sid, body_start, body_end, 7, SIGNED_CURRENCY_FMT),   # H realized P/L
        _num_format_req(sid, total_idx, total_idx + 1, 7, SIGNED_CURRENCY_FMT),
        _align_center_req(sid, 1, total_idx + 1, cols),
        _header_fill_req(sid, 1, cols),
        _border_grid_req(sid, 1, body_end, cols),
        _border_grid_req(sid, total_idx, total_idx + 1, cols),
        _bold_row_req(sid, 1),
        _bold_row_req(sid, total_idx),
    ]
    fmt.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"italic": True, "foregroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}}},
        "fields": "userEnteredFormat.textFormat.italic,userEnteredFormat.textFormat.foregroundColor",
    }})
    write_tab(svc, sheet_id, REALIZED_TAB, rows, format_requests=fmt)
    log.info("Wrote %s tab: %d trade(s), total realized $%s",
             REALIZED_TAB, len(trades), f"{round(total):,}")


def main() -> int:
    sheet_id = env("SHEET_ID", required=True)
    account_number = env("ROBINHOOD_ACCOUNT_NUMBER") or None

    login()
    positions = fetch_positions(account_number)
    stocks = fetch_stock_positions(account_number)
    buying_power, options_collateral, portfolio_cash = fetch_account_balances(account_number)

    svc = sheets_service()
    ensure_tab(svc, sheet_id, POSITIONS_TAB)
    ensure_tab(svc, sheet_id, SUMMARY_TAB)

    # Fetch initial invested amount from Summary!B1 (defaults to 45000.0)
    initial_invested = 45000.0
    try:
        res = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"{SUMMARY_TAB}!B1").execute()
        vals = res.get("values", [])
        if vals and vals[0] and vals[0][0]:
            initial_invested = _as_float(vals[0][0]) or 45000.0
    except Exception as e:
        log.warning("Failed to fetch initial invested amount from Summary!B1: %s. Defaulting to 45000.0", e)

    sheet_rows, meta = build_sheet(positions, stocks, buying_power, options_collateral, portfolio_cash, initial_invested)
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

    # Format requests for K2:L4 cash metrics block (row 2-4, columns K-L)
    fmt_requests.append(_num_format_req(sid, 1, 4, 11, '"$"#,##0'))
    fmt_requests.append({
        "repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 10, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat.textFormat.bold,userEnteredFormat.horizontalAlignment,userEnteredFormat.verticalAlignment",
        }
    })
    line = {"style": "SOLID", "color": {"red": 0.7, "green": 0.7, "blue": 0.7}}
    fmt_requests.append({
        "updateBorders": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 10, "endColumnIndex": 12},
            "top": line, "bottom": line, "left": line, "right": line,
            "innerHorizontal": line, "innerVertical": line,
        }
    })

    # Column M risk flags: bold red so at-risk long options are impossible to miss.
    if meta.get("flag_rows"):
        fmt_requests.append({"repeatCell": {
            "range": {"sheetId": sid,
                      "startRowIndex": meta["opt_body"][0], "endRowIndex": meta["opt_body"][1],
                      "startColumnIndex": FLAG_COL, "endColumnIndex": FLAG_COL + 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True, "foregroundColor": {"red": 0.80, "green": 0.15, "blue": 0.15}},
                "horizontalAlignment": "LEFT"}},
            "fields": ("userEnteredFormat.textFormat.bold,"
                       "userEnteredFormat.textFormat.foregroundColor,"
                       "userEnteredFormat.horizontalAlignment"),
        }})

    write_tab(svc, sheet_id, POSITIONS_TAB, sheet_rows, format_requests=fmt_requests)
    log.info("Wrote Positions tab: %d option(s), %d stock(s) + totals%s",
             len(positions), len(stocks),
             f", {len(meta['flag_rows'])} risk flag(s)" if meta.get("flag_rows") else "")

    # Write Options Allocation and Stocks Allocation to Summary!G1:H2
    opt_cost_sum = sum(abs(p["avg_price"]) * abs(p["quantity"]) for p in positions)
    stk_cost_sum = sum(s["avg_cost"] * s["quantity"] for s in stocks)
    options_alloc = opt_cost_sum / initial_invested if initial_invested > 0 else 0.0
    stocks_alloc = stk_cost_sum / initial_invested if initial_invested > 0 else 0.0

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{SUMMARY_TAB}!G1:H2",
        valueInputOption="USER_ENTERED",
        body={"values": [
            ["Options", options_alloc],
            ["Stocks", stocks_alloc]
        ]},
    ).execute()

    # Clear Summary!G3:H4 (indices: startRow 2, endRow 4, startCol 6, endCol 8)
    summary_sid = _sheet_id_int(svc, sheet_id, SUMMARY_TAB)
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [
        {"updateCells": {
            "range": {"sheetId": summary_sid, "startRowIndex": 2, "endRowIndex": 4, "startColumnIndex": 6, "endColumnIndex": 8},
            "fields": "userEnteredFormat,userEnteredValue",
        }}
    ]}).execute()

    # Apply formatting and borders to G1:H2
    line_border = {"style": "SOLID", "color": {"red": 0.7, "green": 0.7, "blue": 0.7}}
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [
        # Format H1:H2 as percentage
        {
            "repeatCell": {
                "range": {"sheetId": summary_sid, "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 7, "endColumnIndex": 8},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.0%"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        # Bold and Center G1:H2
        {
            "repeatCell": {
                "range": {"sheetId": summary_sid, "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 6, "endColumnIndex": 8},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
                "fields": "userEnteredFormat.textFormat.bold,userEnteredFormat.horizontalAlignment,userEnteredFormat.verticalAlignment",
            }
        },
        # Borders around G1:H2
        {
            "updateBorders": {
                "range": {"sheetId": summary_sid, "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 6, "endColumnIndex": 8},
                "top": line_border, "bottom": line_border, "left": line_border, "right": line_border,
                "innerHorizontal": line_border, "innerVertical": line_border,
            }
        }
    ]}).execute()
    log.info("Wrote Options and Stock allocations to Summary!G1:H2 and cleared Summary!G3:H4")

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

    # Realized P/L reconstructed from order history -> its own script-owned tab.
    # The hand-kept "Closed Positions" tab is intentionally NEVER touched: it
    # holds pre-Feb-2026 and other-sub-account trades that cannot be rebuilt.
    ensure_tab(svc, sheet_id, REALIZED_TAB)
    write_realized_tab(svc, sheet_id, fetch_realized_trades(account_number))

    return 0


if __name__ == "__main__":
    # main() returning 0 raises SystemExit(0) (not an Exception) → clean success.
    # Any real failure (notably an expired Robinhood session, which surfaces as
    # "... can only be called when logged in") is caught, alerted, and re-signalled
    # as a non-zero exit so cron/bot runs don't fail silently.
    try:
        sys.exit(main())
    except Exception as e:
        notify_failure(e)
        log.error("Sync failed: %s", e)
        sys.exit(1)
