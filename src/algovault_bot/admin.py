"""Admin /stats handler — gated by BOT_ADMIN_CHAT_IDS env var (CSV of chat_ids).

Produces totals: subscribers, alerts (24h/7d/all-time, split regime/calls),
CTAs shown (by campaign), top 10 watched assets. UTM-attribution pull from
signal-MCP /api/usage-stats is dropped under D2-B (endpoint is fictional;
attribution lives Plausible-side, validated operator-side).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from .db import Database


log = logging.getLogger(__name__)


def is_admin(chat_id: int) -> bool:
    raw = os.environ.get("BOT_ADMIN_CHAT_IDS", "").strip()
    if not raw:
        return False
    allowed: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            allowed.add(int(part))
        except ValueError:
            continue
    return chat_id in allowed


def render_stats(db: Database) -> str:
    """Return a formatted multi-line stats string for the requesting admin."""
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()

    with db._cursor() as cur:
        # BOT-ZOMBIE-W1 2026-05-17: Total excludes bot-blocked subscribers.
        cur.execute(
            "SELECT COUNT(*) FROM subscribers WHERE bot_blocked_at IS NULL"
        )
        total_subs = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM subscribers WHERE bot_blocked_at IS NOT NULL"
        )
        blocked = int(cur.fetchone()[0])

        # BOT-DIGEST-QUOTA-NOTICES-W1 2026-06-15: exclude watchlist rows owned
        # by bot-blocked subscribers (parity with the daily digest) — keeps the
        # headline count + the Top-10 breakdown below internally consistent.
        cur.execute(
            "SELECT COUNT(*) FROM watchlists w "
            "JOIN subscribers s ON s.chat_id = w.chat_id "
            "WHERE s.bot_blocked_at IS NULL"
        )
        total_watches = int(cur.fetchone()[0])

        # BOT-DIGEST-LAST24H-W1 2026-05-21: rolling-24h alert counts from
        # the alerts_fired log; matches the daily-digest shape so digest +
        # admin stay aligned (BOT-ZOMBIE-W1 precedent).
        cur.execute(
            "SELECT kind, COUNT(*) FROM alerts_fired "
            "WHERE fired_at >= datetime('now', '-1 day') "
            "GROUP BY kind"
        )
        kind_counts = {row[0]: int(row[1]) for row in cur.fetchall()}
        regime_24h = kind_counts.get("regime", 0)
        # BOT-DIGEST-COUNT-ALL-CALLS-W1: /stats mirrors the daily digest — break 📈 Calls
        # out by delivery source (Watch · Scanwatch · Scan); C = w + sw + sc.
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

        # BOT-DIGEST-QUOTA-NOTICES-W1 2026-06-15: rolling-24h quota-exhausted
        # notice count (matches the daily-digest line; digest + /stats aligned).
        cur.execute(
            "SELECT COUNT(*) FROM quota_notices_fired "
            "WHERE fired_at >= datetime('now', '-1 day')"
        )
        quota_notices_24h = int(cur.fetchone()[0])

        # Lifetime per-user totals (kept for admin-only deep view + the
        # CTAs→Linked conversion ratio computed below).
        cur.execute("SELECT COALESCE(SUM(total_regime_alerts), 0) FROM subscribers")
        regime_alerts_total = int(cur.fetchone()[0])
        cur.execute("SELECT COALESCE(SUM(total_call_alerts), 0) FROM subscribers")
        call_alerts_total = int(cur.fetchone()[0])
        cur.execute("SELECT COALESCE(SUM(total_ctas_shown), 0) FROM subscribers")
        ctas_total = int(cur.fetchone()[0])

        # BOT-W2: conversion attribution — linked subscribers + tier breakdown
        cur.execute(
            "SELECT COUNT(*) FROM subscribers WHERE linked_api_key IS NOT NULL"
        )
        linked_total = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT COALESCE(linked_tier, '?') AS tier, COUNT(*) AS c
            FROM subscribers
            WHERE linked_api_key IS NOT NULL
            GROUP BY linked_tier
            ORDER BY c DESC, tier ASC
            """
        )
        linked_by_tier = list(cur.fetchall())

        # Top 10 watched assets
        cur.execute(
            """
            SELECT w.coin, COUNT(*) AS c
            FROM watchlists w
            JOIN subscribers s ON s.chat_id = w.chat_id
            WHERE s.bot_blocked_at IS NULL
            GROUP BY w.coin
            ORDER BY c DESC, w.coin ASC
            LIMIT 10
            """
        )
        top_assets = list(cur.fetchall())

        # Operator format alignment 2026-05-10: replaced "active 24h / 7d"
        # retention metrics with "new subscribers last 24h" acquisition
        # metric to match the daily digest format. BOT-ZOMBIE-W1 2026-05-17:
        # excludes bot-blocked subscribers so the count reflects reachable
        # users.
        cur.execute(
            "SELECT COUNT(*) FROM subscribers "
            "WHERE created_at >= ? AND bot_blocked_at IS NULL",
            (day_ago,),
        )
        new_subs_24h = int(cur.fetchone()[0])

    # BOT-W2 conversion attribution. Compute ratio CTAs-shown → signups-linked.
    conversion_ratio = (
        f"{(linked_total / ctas_total * 100):.1f}%"
        if ctas_total > 0 else "n/a"
    )

    lines = [
        "📊 Algovault-Telegram-bot — Admin Stats",
        f"Generated: {now.isoformat(timespec='seconds')}",
        "",
        f"👥 Total Subscribers: {total_subs}",
        f"👥 New Subscribers last 24h: {new_subs_24h}",
    ]
    if blocked > 0:
        lines.append(f"🚫 Blocked the bot: {blocked}")
    lines.extend([
        f"📝 Watchlist entries : {total_watches}",
        "",
        "Last 24h Alerts:",
        f"  📊 Regime: {regime_24h}",
        f"  📈 Calls: {calls_24h}  "
        f"(👁 Watch {calls_watch} · 🔭 Scanwatch {calls_scanwatch} · 🔎 Scan {calls_scan})",
        f"  🔒 Quota-exhausted notices: {quota_notices_24h}",
        "",
        "🔔 Alerts (lifetime, per-user counters):",
        f"  📊 Regime shifts   : {regime_alerts_total}",
        f"  📈 Trade calls     : {call_alerts_total}",
        f"  🎯 CTAs shown      : {ctas_total}",
        "",
        "💎 Conversion attribution (BOT-W2):",
        f"  Linked subscribers : {linked_total}",
    ])
    for r in linked_by_tier:
        lines.append(f"    {r['tier']:<10} : {r['c']}")
    if not linked_by_tier:
        lines.append("    (none yet)")
    lines.append(f"  CTAs → linked    : {conversion_ratio}")
    lines.append("")
    lines.append("Top 10 watched assets:")
    for r in top_assets:
        lines.append(f"  {r['coin']:<8} {r['c']}")
    if not top_assets:
        lines.append("  (none)")
    lines.append("")
    lines.append(
        "Note: UTM attribution rollups live in Plausible (algovault.com analytics) — "
        "filter by `utm_source=tg_bot`."
    )
    return "\n".join(lines)


def handle_stats(db: Database, chat_id: int) -> str:
    """Return the stats string if the chat is an admin, else 'Not authorized.'"""
    if not is_admin(chat_id):
        return "Not authorized."
    return render_stats(db)
