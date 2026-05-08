"""Nightly admin digest (C5) — fires at 03:00 UTC via systemd timer.

Sends a one-line summary to Mr.1's INTERNAL Telegram chat (signal-MCP's
existing monitor pipeline; TOKEN + CHAT_ID injected by the systemd unit
from /opt/crypto-quant-signal-mcp/.env). NOT the public bot's token —
the digest is operator-only.

Reuses the existing internal-monitor edge per the system-map; the public
bot stays scoped to public subscribers.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

from .db import Database, DEFAULT_DB_PATH


log = logging.getLogger("algovault_bot.digest")


def render_digest(db: Database) -> str:
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()

    with db._cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM subscribers")
        total_subs = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM subscribers WHERE last_seen_at >= ?", (day_ago,))
        active_24h = int(cur.fetchone()[0])

        cur.execute("SELECT COALESCE(SUM(total_regime_alerts), 0) FROM subscribers")
        regime_total = int(cur.fetchone()[0])
        cur.execute("SELECT COALESCE(SUM(total_call_alerts), 0) FROM subscribers")
        calls_total = int(cur.fetchone()[0])

        cur.execute("SELECT COUNT(*) FROM watchlists")
        watch_total = int(cur.fetchone()[0])

    return (
        "🤖 algovault-bot — daily digest "
        f"({now.strftime('%Y-%m-%d %H:%M UTC')})\n"
        f"\n"
        f"👥 Subscribers: {total_subs} (active 24h: {active_24h})\n"
        f"📝 Watchlist entries: {watch_total}\n"
        f"\n"
        f"All-time alerts:\n"
        f"  📊 Regime: {regime_total}\n"
        f"  📈 Calls: {calls_total}\n"
    )


def send_via_internal_monitor(text: str) -> None:
    """Send a message via signal-MCP's internal monitor Telegram bot.

    Reads ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` from the environment;
    these come from ``/opt/crypto-quant-signal-mcp/.env`` via the systemd
    unit's ``EnvironmentFile=`` directive.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping digest send")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = httpx.post(
            url,
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
            timeout=15.0,
        )
        if resp.status_code != 200:
            log.error("digest send failed: HTTP %s — %s", resp.status_code, resp.text[:200])
        else:
            log.info(json.dumps({"event": "digest_sent", "chat_id": chat_id}))
    except httpx.HTTPError as e:
        log.error("digest send raised: %s", e)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # CRITICAL: silence httpx — its INFO log line includes the full request
    # URL, which for Telegram looks like `bot<TOKEN>/sendMessage`. Anything
    # short of WARNING leaks the bearer token into journald + logrotate-archived
    # logs. Discovered live during BOT-W1 C5 first digest fire (2026-05-08).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    db_path = os.environ.get("ALGOVAULT_BOT_DB_PATH", DEFAULT_DB_PATH)
    db = Database(db_path)
    text = render_digest(db)
    log.info("digest_render: %s", text.replace("\n", " | "))
    send_via_internal_monitor(text)


if __name__ == "__main__":
    main()
