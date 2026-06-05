"""Telegram trigger for the options sync.

Long-polls Telegram for commands and, when an authorized user sends /sync,
runs ./sync and replies with the result. Runs as a systemd service on the VM
(see options-sync-bot.service.example) so the sync can be triggered on demand
from any device — phone, laptop, or web Telegram — independent of your Mac.

Reads from .env:
  TELEGRAM_BOT_TOKEN        - from @BotFather
  TELEGRAM_ALLOWED_CHAT_ID  - who may trigger /sync. Use "*" to allow anyone,
                              or a comma-separated list of chat IDs to allow
                              only those. Empty = nobody (fails closed). /id
                              always works so you can discover a chat ID.

Uses only the standard library (plus python-dotenv, already a project dep).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg-bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

# Lines from the sync log worth relaying back to the user.
_INTERESTING = ("returned", "Wrote Positions", "Summary!", "Error",
                "Traceback", "Authentication", "MFA")


def _api(method: str, params: dict | None = None, timeout: int = 60) -> dict:
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def send(chat_id: int, text: str) -> None:
    try:
        _api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        log.warning("sendMessage failed: %s", e)


def run_sync() -> tuple[bool, str]:
    """Run ./sync and return (ok, short_summary)."""
    try:
        p = subprocess.run(
            ["./sync"], cwd=str(PROJECT_DIR),
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "Sync timed out after 180s."
    except Exception as e:
        return False, f"Failed to launch sync: {e}"
    out = (p.stdout or "") + (p.stderr or "")
    lines = [ln for ln in out.splitlines() if any(k in ln for k in _INTERESTING)]
    summary = "\n".join(lines[-8:]) if lines else (out[-500:] or "(no output)")
    return p.returncode == 0, summary


def authorized(chat_id: int) -> bool:
    # "*" opens the bot to anyone. Otherwise ALLOWED is a comma-separated
    # allowlist of chat IDs; empty = fail closed (nobody authorized).
    if ALLOWED == "*":
        return True
    allowed = {x.strip() for x in ALLOWED.split(",") if x.strip()}
    return str(chat_id) in allowed


def handle(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].lower().lstrip("/").split("@")[0] if text else ""

    # /id always works (no auth) so you can discover your chat ID for lockdown.
    if cmd == "id":
        send(chat_id, f"Your chat ID is: <code>{chat_id}</code>")
        return

    if not authorized(chat_id):
        send(chat_id, "⛔ Not authorized to trigger this bot.")
        log.warning("Unauthorized message from chat_id=%s (text=%r)", chat_id, text[:40])
        return

    if cmd in ("start", "help", ""):
        send(chat_id, "📈 Options sync bot.\nSend /sync to refresh your Robinhood "
                      "positions + cash in the Google Sheet.")
    elif cmd == "sync":
        send(chat_id, "⏳ Running sync…")
        ok, summary = run_sync()
        prefix = "✅ Sync complete" if ok else "❌ Sync failed"
        send(chat_id, f"{prefix}\n<pre>{summary}</pre>")
    else:
        send(chat_id, "Unknown command. Send /sync or /help.")


def main() -> int:
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set in .env — cannot start.")
        return 2
    mode = "ANYONE (open)" if ALLOWED == "*" else (ALLOWED or "(none — /sync disabled)")
    log.info("Bot starting. Authorized: %s", mode)
    offset = None
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            resp = _api("getUpdates", params, timeout=60)
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle(upd)
                except Exception as e:
                    log.exception("handler error: %s", e)
        except Exception as e:
            log.warning("poll error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
