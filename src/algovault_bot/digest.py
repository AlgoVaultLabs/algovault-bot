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
from datetime import datetime, timedelta, timezone

import httpx

from .db import Database, DEFAULT_DB_PATH


log = logging.getLogger("algovault_bot.digest")


def render_digest(db: Database) -> str:
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()

    with db._cursor() as cur:
        # BOT-ZOMBIE-W1 2026-05-17: "Total" + "New 24h" both exclude
        # bot-blocked subscribers so the count reflects reachable users.
        # The Blocked line surfaces only when at least one zombie exists.
        cur.execute(
            "SELECT COUNT(*) FROM subscribers WHERE bot_blocked_at IS NULL"
        )
        total_subs = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM subscribers "
            "WHERE created_at >= ? AND bot_blocked_at IS NULL",
            (day_ago,),
        )
        new_subs_24h = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM subscribers WHERE bot_blocked_at IS NOT NULL"
        )
        blocked = int(cur.fetchone()[0])

        cur.execute("SELECT COALESCE(SUM(total_regime_alerts), 0) FROM subscribers")
        regime_total = int(cur.fetchone()[0])
        cur.execute("SELECT COALESCE(SUM(total_call_alerts), 0) FROM subscribers")
        calls_total = int(cur.fetchone()[0])

        cur.execute("SELECT COUNT(*) FROM watchlists")
        watch_total = int(cur.fetchone()[0])

    lines = [
        "🤖 Algovault-Telegram-bot — Daily Digest "
        f"({now.strftime('%Y-%m-%d %H:%M UTC')})",
        "",
        f"👥 Total Subscribers: {total_subs}",
        f"👥 New Subscribers last 24h: {new_subs_24h}",
    ]
    if blocked > 0:
        lines.append(f"🚫 Blocked the bot: {blocked}")
    lines.extend([
        "",
        f"📝 Watchlist entries: {watch_total}",
        "",
        "All-time alerts:",
        f"  📊 Regime: {regime_total}",
        f"  📈 Calls: {calls_total}",
        "",
    ])
    return "\n".join(lines)


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
