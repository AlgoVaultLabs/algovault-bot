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
    week_ago = (now - timedelta(days=7)).isoformat()

    with db._cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM subscribers")
        total_subs = int(cur.fetchone()[0])

        cur.execute("SELECT COUNT(*) FROM watchlists")
        total_watches = int(cur.fetchone()[0])

        # All-time totals
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
            SELECT coin, COUNT(*) AS c
            FROM watchlists
            GROUP BY coin
            ORDER BY c DESC, coin ASC
            LIMIT 10
            """
        )
        top_assets = list(cur.fetchall())

        # Subscribers seen in 24h / 7d
        cur.execute("SELECT COUNT(*) FROM subscribers WHERE last_seen_at >= ?", (day_ago,))
        active_24h = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM subscribers WHERE last_seen_at >= ?", (week_ago,))
        active_7d = int(cur.fetchone()[0])

    # BOT-W2 conversion attribution. Compute ratio CTAs-shown → signups-linked.
    conversion_ratio = (
        f"{(linked_total / ctas_total * 100):.1f}%"
        if ctas_total > 0 else "n/a"
    )

    lines = [
        "📊 algovault-bot — admin stats",
        f"Generated: {now.isoformat(timespec='seconds')}",
        "",
        f"👥 Subscribers       : {total_subs} (active 24h={active_24h}, 7d={active_7d})",
        f"📝 Watchlist entries : {total_watches}",
        "",
        "🔔 Alerts (all-time, per-user counters):",
        f"  📊 Regime shifts   : {regime_alerts_total}",
        f"  📈 Trade calls     : {call_alerts_total}",
        f"  🎯 CTAs shown      : {ctas_total}",
        "",
        "💎 Conversion attribution (BOT-W2):",
        f"  Linked subscribers : {linked_total}",
    ]
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
