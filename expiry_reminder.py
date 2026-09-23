"""Telegram reminder for option contracts expiring soon.

Why this exists: the realized-P/L review (2026-09-23) found that 8 contracts
which expired worthless cost **$12,038** — seven of them LONG calls that went
to zero, several sitting at deep losses for weeks (WPM was at -98% with time
still on the clock). The column-M flag on the Positions tab helps, but it is
passive: it only works if you happen to open the sheet. This pushes the same
information at you while you can still act.

Run it on a weekday cron; it stays silent unless something is actually expiring
inside EXPIRY_REMINDER_DAYS, so it never becomes noise you learn to ignore.

Long vs short matters and is called out explicitly:
  * LONG  expiring underwater -> a decision (close for salvage, or accept zero)
  * SHORT expiring worthless  -> that's the win condition, nothing to do

Env:
  EXPIRY_REMINDER_DAYS   how far ahead to look (default 3 calendar days)
  TELEGRAM_BOT_TOKEN / TELEGRAM_ALERT_CHAT_ID   delivery (shared with alerts)
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sync_positions import (env, fetch_positions, log, login, notify_failure,
                            telegram_send)


def _fmt_money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _when(days: int, expiry: str) -> str:
    if days == 0:
        return "TODAY"
    if days == 1:
        return "TOMORROW"
    return f"in {days}d ({expiry})"


def build_message(positions: list[dict], horizon: int, today: date) -> str:
    """Return the reminder text, or '' when nothing is expiring soon."""
    soon = []
    for p in positions:
        try:
            days = (date.fromisoformat(str(p["expiration"])) - today).days
        except (TypeError, ValueError, KeyError):
            continue
        if 0 <= days <= horizon:
            soon.append((days, p))
    if not soon:
        return ""

    soon.sort(key=lambda x: (x[0], x[1]["underlying"]))
    longs = [(d, p) for d, p in soon if p["quantity"] > 0]
    shorts = [(d, p) for d, p in soon if p["quantity"] < 0]

    lines = [f"⏳ Expiring soon — {len(soon)} contract(s)"]

    if longs:
        lines.append("\n🔴 LONG — these go to $0 if you do nothing:")
        for days, p in longs:
            avg = abs(p["avg_price"])
            qty = abs(p["quantity"])
            cur = p.get("current")
            head = (f"• {p['underlying']} {p['strike']:g}{p['type']} ×{qty:g} — "
                    f"{_when(days, p['expiration'])}")
            if cur is None:
                lines.append(head + "\n   (no live price)")
                continue
            pl = (cur - avg) * qty
            pct = (pl / (avg * qty) * 100) if avg else 0.0
            lines.append(f"{head}\n   now ${cur:,.2f} vs paid ${avg:,.2f} → "
                         f"{_fmt_money(pl)} ({pct:+.0f}%)")

    # Split shorts by whether they're actually on track. A short that is
    # UNDERWATER near expiry is in the money and heads for ASSIGNMENT, not a
    # free expiry — calling that "the win" would be actively misleading (it is
    # how the account picked up its assigned stock positions).
    def _short_pl(p):
        cur = p.get("current")
        if cur is None:
            return None
        return (abs(p["avg_price"]) - cur) * abs(p["quantity"])

    at_risk = [(d, p) for d, p in shorts if (_short_pl(p) or 0) < 0]
    on_track = [(d, p) for d, p in shorts if (_short_pl(p) or 0) >= 0]

    def _short_lines(group, note):
        out = []
        for days, p in group:
            avg = abs(p["avg_price"])
            qty = abs(p["quantity"])
            cur = p.get("current")
            head = (f"• {p['underlying']} {p['strike']:g}{p['type']} ×{qty:g} — "
                    f"{_when(days, p['expiration'])}")
            if cur is None:
                out.append(head + "\n   (no live price)")
                continue
            out.append(f"{head}\n   now ${cur:,.2f} vs sold ${avg:,.2f} → "
                       f"{_fmt_money((avg - cur) * qty)}{note}")
        return out

    if at_risk:
        lines.append("\n🟠 SHORT, in the money — assignment risk:")
        lines += _short_lines(at_risk, "")
    if on_track:
        lines.append("\n🟢 SHORT, on track — expiring worthless is the win:")
        lines += _short_lines(on_track, "")

    if longs:
        lines.append("\nDeep-underwater longs are what cost $12,038 last cycle — "
                     "decide rather than let them lapse.")
    if at_risk:
        lines.append("In-the-money shorts will likely be assigned — roll or close "
                     "if you don't want the shares.")
    return "\n".join(lines)


def main() -> int:
    account_number = env("ROBINHOOD_ACCOUNT_NUMBER") or None
    try:
        horizon = int(env("EXPIRY_REMINDER_DAYS", default="3") or 3)
    except ValueError:
        horizon = 3

    login()
    positions = fetch_positions(account_number)
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()

    text = build_message(positions, horizon, today)
    if not text:
        log.info("Nothing expiring within %dd — staying quiet", horizon)
        return 0
    telegram_send(text)
    log.info("Sent expiry reminder (horizon %dd)", horizon)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify_failure(e)
        log.error("Expiry reminder failed: %s", e)
        sys.exit(1)
