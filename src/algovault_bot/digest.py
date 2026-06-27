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

        # BOT-DIGEST-LAST24H-W1 2026-05-21: switched from
        # SUM(total_regime_alerts) / SUM(total_call_alerts) lifetime counters
        # to a rolling-24h count over the alerts_fired log so the digest
        # answers "what happened yesterday" instead of "what happened since
        # /opt/algovault-bot/ was first deployed". Lifetime totals still live
        # in admin /stats under a separate "(all-time)" header.
        cur.execute(
            "SELECT kind, COUNT(*) FROM alerts_fired "
            "WHERE fired_at >= datetime('now', '-1 day') "
            "GROUP BY kind"
        )
        kind_counts = {row[0]: int(row[1]) for row in cur.fetchall()}
        regime_24h = kind_counts.get("regime", 0)
        # BOT-DIGEST-COUNT-ALL-CALLS-W1: break 📈 Calls out by delivery source so a
        # silently-zeroed path is visible at a glance (the bug this fixed: scanwatch +
        # scan delivered calls but never logged → Calls undercounted). C = w + sw + sc.
        cur.execute(
            "SELECT source, COUNT(*) FROM alerts_fired "
            "WHERE kind='call' AND fired_at >= datetime('now', '-1 day') "
            "GROUP BY source"
        )
        src_counts = {row[0]: int(row[1]) for row in cur.fetchall()}
        calls_watch = src_counts.get("watch", 0)
        calls_scanwatch = src_counts.get("scanwatch", 0)
        calls_scan = src_counts.get("scan", 0)
        calls_24h = calls_watch + calls_scanwatch + calls_scan

        # BOT-DIGEST-QUOTA-NOTICES-W1 2026-06-15: exclude watchlist rows owned
        # by bot-blocked subscribers — they can never receive an alert, so
        # counting them overstated reachable watchers. (watchlists.chat_id is
        # FK→subscribers, so the JOIN never drops a real row.)
        cur.execute(
            "SELECT COUNT(*) FROM watchlists w "
            "JOIN subscribers s ON s.chat_id = w.chat_id "
            "WHERE s.bot_blocked_at IS NULL"
        )
        watch_total = int(cur.fetchone()[0])

        # BOT-DIGEST-QUOTA-NOTICES-W1 2026-06-15: rolling-24h count of
        # quota-exhausted notices delivered (a watcher hit the 100/mo free
        # cap). Surfaced so "Calls: 0" isn't misread as "quiet market" when
        # really the only active watcher is quota-capped. Separate table from
        # alerts_fired (UX-nudge volume, not signal volume).
        cur.execute(
            "SELECT COUNT(*) FROM quota_notices_fired "
            "WHERE fired_at >= datetime('now', '-1 day')"
        )
        quota_notices_24h = int(cur.fetchone()[0])

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
        "Last 24h Alerts:",
        f"  📊 Regime: {regime_24h}",
        f"  📈 Calls: {calls_24h}  "
        f"(👁 Watch {calls_watch} · 🔭 Scanwatch {calls_scanwatch} · 🔎 Scan {calls_scan})",
        f"  🔒 Quota-exhausted notices: {quota_notices_24h}",
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
