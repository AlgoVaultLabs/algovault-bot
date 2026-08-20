"""Nightly admin digest (C5) — fires at 03:00 UTC via systemd timer.

Sends a one-line summary to Mr.1's INTERNAL Telegram chat (signal-MCP's
existing monitor pipeline; TOKEN + CHAT_ID injected by the systemd unit
from /opt/crypto-quant-signal-mcp/.env). NOT the public bot's token —
the digest is operator-only.

Reuses the existing internal-monitor edge per the system-map; the public
bot stays scoped to public subscribers.

OPS-DIGEST-TGBOT-METRIC-BRIDGE-W1 (2026-07-06): the digest's numbers are now
computed ONCE into a ``DigestMetrics`` (``compute_digest_metrics``) that feeds
BOTH the Telegram string (``render_digest``) AND a shared-Postgres
``bot_daily_metrics`` upsert (``write_bot_daily_metrics``) — single-derivation,
so the bot's own digest and the main 📊 AlgoVault Daily Digest's ``🔁 TG bot``
line can never disagree. The Postgres write is FAIL-SOFT: it never raises and
never blocks the bot's own digest send.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .db import Database, DEFAULT_DB_PATH
from .quota import count_walled_now


log = logging.getLogger("algovault_bot.digest")


@dataclass(frozen=True)
class DigestMetrics:
    """One-shot snapshot of the daily digest numbers. The SINGLE source both the
    Telegram render and the shared-Postgres ``bot_daily_metrics`` row derive from."""

    metric_date: str  # 'YYYY-MM-DD' UTC (digest run date; the 24h window ends at run time)
    total_subs: int
    new_subs_24h: int
    blocked: int
    regime_24h: int
    calls_watch: int
    calls_scanwatch: int
    calls_scan: int
    calls_24h: int  # = watch + scanwatch + scan
    watch_total: int
    quota_notices_24h: int
    # BOT-QUOTA-REFUSAL-SEAM-W1: point-in-time STATE, not a 24h window — how many
    # free subscribers are sitting behind the wall right now, and how many of those
    # have never been told. `walled_silent > 0` is by definition a seam defect.
    walled_now: int
    walled_silent: int
    # PRICING-BOT-DELIVERY-METERING-W1 CH5f — of the walled, how many are PAYING. A walled paid
    # subscriber is revenue at its ceiling, not a conversion opportunity: a different signal.
    # No default: every field after it lacks one, and a defaulted field cannot precede those.
    walled_paid: int
    # OPS-DIGEST-TGBOT-TIER-AND-WALLED-W1: delivered calls whose owner is on a PAID tier.
    # Bot traffic authenticates as `tier:'internal'`, so a paying subscriber's alerts can
    # never reach the operator digest's 💳 Paid row (which counts direct API/MCP calls and
    # excludes `is_bot_internal`). Without this the digest reads "Paid: 0" on a day when
    # paying users took 205 alerts. Reported in the BOT's unit — delivered alerts — beside
    # the TG bot row, never folded into the API's Paid count: different quantities.
    calls_paid_linked: int
    # PRICING-BOT-DELIVERY-METERING-W1 CH6e — the plan-metering rail's own health.
    # `outbox_pending` is the one to watch: a queue that stops draining is revenue quietly
    # not being charged, and it is invisible everywhere else.
    plan_units_debited: int
    outbox_pending: int
    generated_at: str  # ISO-8601 UTC


def compute_digest_metrics(db: Database) -> DigestMetrics:
    """Run the digest queries ONCE. Verbatim from the pre-W1 ``render_digest`` body
    (same SQL, same windows) so the bridged row == the bot's own digest numbers."""
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()

    with db._cursor() as cur:
        # BOT-ZOMBIE-W1 2026-05-17: "Total" + "New 24h" both exclude
        # bot-blocked subscribers so the count reflects reachable users.
        cur.execute("SELECT COUNT(*) FROM subscribers WHERE bot_blocked_at IS NULL")
        total_subs = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM subscribers "
            "WHERE created_at >= ? AND bot_blocked_at IS NULL",
            (day_ago,),
        )
        new_subs_24h = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM subscribers WHERE bot_blocked_at IS NOT NULL")
        blocked = int(cur.fetchone()[0])

        # BOT-DIGEST-LAST24H-W1 2026-05-21: rolling-24h counts over the alerts_fired log.
        cur.execute(
            "SELECT kind, COUNT(*) FROM alerts_fired "
            "WHERE fired_at >= datetime('now', '-1 day') "
            "GROUP BY kind"
        )
        kind_counts = {row[0]: int(row[1]) for row in cur.fetchall()}
        regime_24h = kind_counts.get("regime", 0)
        # BOT-DIGEST-COUNT-ALL-CALLS-W1: 📈 Calls broken out by delivery source. C = w + sw + sc.
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

        # Same window + same predicate as the per-source counts above, split by the owner's
        # tier. `linked_tier IS NOT NULL` is the paid predicate: quota.PAID_TIERS is the SoT
        # for which tiers bypass, and every value that column holds is in it.
        cur.execute(
            "SELECT COUNT(*) FROM alerts_fired a JOIN subscribers s ON s.chat_id = a.chat_id "
            "WHERE a.kind='call' AND a.fired_at >= datetime('now', '-1 day') "
            "AND s.linked_tier IS NOT NULL"
        )
        calls_paid_linked = int(cur.fetchone()[0])

        # BOT-DIGEST-QUOTA-NOTICES-W1 2026-06-15: reachable watchers only.
        cur.execute(
            "SELECT COUNT(*) FROM watchlists w "
            "JOIN subscribers s ON s.chat_id = w.chat_id "
            "WHERE s.bot_blocked_at IS NULL"
        )
        watch_total = int(cur.fetchone()[0])

        # BOT-DIGEST-QUOTA-NOTICES-W1 2026-06-15: rolling-24h quota-exhausted notices.
        cur.execute(
            "SELECT COUNT(*) FROM quota_notices_fired "
            "WHERE fired_at >= datetime('now', '-1 day')"
        )
        quota_notices_24h = int(cur.fetchone()[0])

    # BOT-QUOTA-REFUSAL-SEAM-W1: derived by projecting every reachable subscriber
    # through `evaluate_delivery` — the SAME decision the seam enforces. Never a
    # re-implemented `alert_count >= 100` in SQL, which would be a second derivation
    # of the very thing this wave exists to make single.
    walled_now, walled_silent, walled_paid = count_walled_now(db)
    plan_units_debited = db.count_plan_units_debited_last_24h()
    outbox_pending = db.count_pending_entitlement_debits()

    return DigestMetrics(
        metric_date=now.strftime("%Y-%m-%d"),
        total_subs=total_subs,
        new_subs_24h=new_subs_24h,
        blocked=blocked,
        regime_24h=regime_24h,
        calls_watch=calls_watch,
        calls_scanwatch=calls_scanwatch,
        calls_scan=calls_scan,
        calls_24h=calls_24h,
        watch_total=watch_total,
        quota_notices_24h=quota_notices_24h,
        walled_now=walled_now,
        walled_silent=walled_silent,
        walled_paid=walled_paid,
        calls_paid_linked=calls_paid_linked,
        plan_units_debited=plan_units_debited,
        outbox_pending=outbox_pending,
        generated_at=now.isoformat(),
    )


def _format_digest(m: DigestMetrics) -> str:
    """Render the operator Telegram string from a computed snapshot (verbatim layout)."""
    now_str = datetime.fromisoformat(m.generated_at).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"🤖 Algovault-Telegram-bot — Daily Digest ({now_str})",
        "",
        f"👥 Total Subscribers: {m.total_subs}",
        f"👥 New Subscribers last 24h: {m.new_subs_24h}",
    ]
    if m.blocked > 0:
        lines.append(f"🚫 Blocked the bot: {m.blocked}")
    lines.extend([
        "",
        f"📝 Watchlist entries: {m.watch_total}",
        "",
        "Last 24h Alerts:",
        f"  📊 Regime: {m.regime_24h}",
        f"  📈 Calls: {m.calls_24h}  "
        f"(👁 Watch {m.calls_watch} · 🔭 Scanwatch {m.calls_scanwatch} · 🔎 Scan {m.calls_scan})",
        f"  🔒 Quota-exhausted notices: {m.quota_notices_24h}",
        f"  💳 Plan debits 24h: {m.plan_units_debited}  (⏳ {m.outbox_pending} queued)",
        f"  🚧 Walled now: {m.walled_now}"
        f"  (notified {m.walled_now - m.walled_silent} · silent {m.walled_silent})",
        "",
    ])
    return "\n".join(lines)


def render_digest(db: Database) -> str:
    """Operator digest string. Interface-preserved: derives from the ONE snapshot."""
    return _format_digest(compute_digest_metrics(db))


# ── OPS-DIGEST-TGBOT-METRIC-BRIDGE-W1: shared-Postgres bridge (Option A) ──────

_BOT_METRICS_UPSERT_SQL = """
INSERT INTO bot_daily_metrics
  (metric_date, calls_total, calls_watch, calls_scanwatch, calls_scan,
   alerts_regime, subscribers, new_subscribers_24h, blocked_subscribers,
   watchlist_entries, quota_exhausted_notices,
   calls_paid_linked, walled_now, walled_silent,
   plan_units_debited, outbox_pending, walled_paid_now, generated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (metric_date) DO UPDATE SET
  calls_total=EXCLUDED.calls_total,
  calls_watch=EXCLUDED.calls_watch,
  calls_scanwatch=EXCLUDED.calls_scanwatch,
  calls_scan=EXCLUDED.calls_scan,
  alerts_regime=EXCLUDED.alerts_regime,
  subscribers=EXCLUDED.subscribers,
  new_subscribers_24h=EXCLUDED.new_subscribers_24h,
  blocked_subscribers=EXCLUDED.blocked_subscribers,
  watchlist_entries=EXCLUDED.watchlist_entries,
  quota_exhausted_notices=EXCLUDED.quota_exhausted_notices,
  calls_paid_linked=EXCLUDED.calls_paid_linked,
  walled_now=EXCLUDED.walled_now,
  walled_silent=EXCLUDED.walled_silent,
  plan_units_debited=EXCLUDED.plan_units_debited,
  outbox_pending=EXCLUDED.outbox_pending,
  walled_paid_now=EXCLUDED.walled_paid_now,
  generated_at=EXCLUDED.generated_at
""".strip()


def _bot_metrics_upsert(m: DigestMetrics) -> tuple[str, tuple]:
    """Build the (sql, params) upsert from the SAME snapshot the digest rendered.
    Pure — no I/O — so a test can assert single-derivation (params == metrics)
    without a live Postgres. Param order MUST match ``_BOT_METRICS_UPSERT_SQL``."""
    params = (
        m.metric_date,
        m.calls_24h,
        m.calls_watch,
        m.calls_scanwatch,
        m.calls_scan,
        m.regime_24h,
        m.total_subs,
        m.new_subs_24h,
        m.blocked,
        m.watch_total,
        m.quota_notices_24h,
        m.calls_paid_linked,
        m.walled_now,
        m.walled_silent,
        m.plan_units_debited,
        m.outbox_pending,
        m.walled_paid,
    )
    return _BOT_METRICS_UPSERT_SQL, params


def _redact(text: str, dsn: str) -> str:
    """Scrub the DSN + its bare password from any log/error text (never leak creds —
    a psycopg connection error can echo the DSN). Redacts by STRUCTURE, not prefix."""
    if not text:
        return text
    out = text
    if dsn and dsn in out:
        out = out.replace(dsn, "<dsn-redacted>")
    try:
        from urllib.parse import urlparse

        pw = urlparse(dsn).password or ""
        if pw and pw in out:
            out = out.replace(pw, "<redacted>")
    except Exception:
        pass
    return out


def write_bot_daily_metrics(m: DigestMetrics) -> None:
    """UPSERT the daily snapshot into shared Postgres ``bot_daily_metrics`` (Option A).

    Read by crypto-quant-signal-mcp ``monitor.ts`` for the main digest's ``🔁 TG bot``
    line. FAIL-SOFT: a missing DSN or any write error logs + returns — it MUST NEVER
    raise or block the bot's own digest send. Creds come from ``SIGNAL_PG_DSN`` in
    ``/etc/algovault-bot/env`` (host-only, mode 600, never committed)."""
    dsn = os.environ.get("SIGNAL_PG_DSN", "").strip()
    if not dsn:
        log.info("SIGNAL_PG_DSN unset; skipping bot_daily_metrics write (fail-soft)")
        return
    sql, params = _bot_metrics_upsert(m)
    try:
        import psycopg

        # psycopg3 connection context manager commits on clean exit, rolls back on error.
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            conn.execute(sql, params)
        # Success-path log (load-bearing side-effect proof). No secrets.
        log.info(
            json.dumps({
                "event": "bot_daily_metrics_written",
                "metric_date": m.metric_date,
                "calls_total": m.calls_24h,
                "subscribers": m.total_subs,
            })
        )
    except Exception as e:  # noqa: BLE001 — fail-soft; the digest must survive any PG failure
        log.error(
            "bot_daily_metrics write failed (fail-soft): %s: %s",
            type(e).__name__,
            _redact(str(e), dsn)[:200],
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
    # Single-derivation: compute ONCE → render + send + bridge-write.
    metrics = compute_digest_metrics(db)
    text = _format_digest(metrics)
    log.info("digest_render: %s", text.replace("\n", " | "))
    send_via_internal_monitor(text)
    # AFTER the bot's own digest send — fail-soft, never blocks it.
    write_bot_daily_metrics(metrics)


if __name__ == "__main__":
    main()
