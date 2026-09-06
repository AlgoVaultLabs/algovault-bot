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
import re
import os
from dataclasses import dataclass
from typing import Final
from datetime import datetime, timedelta, timezone

import httpx

from .db import STARS_INTEREST_KIND, Database, DEFAULT_DB_PATH
from .quota import count_walled_now


log = logging.getLogger("algovault_bot.digest")


#: OPS-DEPLOY-PROVENANCE-AND-VERDICT-CLASS-W1 CH3c — where host-deploy.sh stamps the deployed
#: commit. The bot is the WEAKER of the two deploy paths (manual, no GHA), so its provenance is
#: the one more likely to go stale unnoticed.
DEPLOYED_SHA_PATH = "/opt/algovault-bot/DEPLOYED_SHA"


def read_deployed_sha(path: str = DEPLOYED_SHA_PATH) -> str | None:
    """The commit this bot's code was deployed from, or None.

    None means "no provenance recorded" — a real, detectable state the drift canary alerts on.
    It is NEVER substituted with a plausible value: a ref name, a version, or the string "unknown"
    standing in for a commit would recreate the exact defect the provenance surface exists to
    remove. A malformed stamp is also None: a 40-hex sha or nothing.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("sha="):
                    sha = line.split("=", 1)[1].strip()
                    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None
    except OSError:
        return None
    return None


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
    # OPS-VALIDATE-KEY-INDETERMINATE-W1 CH6 — THE DENOMINATOR THE DIGEST NEVER HAD.
    #
    # `plan_units_debited` counts the debits that WORKED. For nine days it read healthy while a
    # `past_due` subscriber's 1,987 debits were stamped terminal and 2,025 alerts went out free,
    # because a rail reporting only its successes cannot report a leak. These two are that leak,
    # in the two units it is visible in: deliveries we will never charge for, and who is in a
    # state that produces them.
    unmetered_24h: int
    linked_by_state: dict[str, int]
    # CH3c — the commit this bot's running code was deployed from. None = no provenance recorded,
    # never a plausible substitute.
    deployed_sha: str | None
    generated_at: str  # ISO-8601 UTC
    # GROWTH-TG-QUOTA-PARITY-W1 follow-up (2026-08-28) — WHICH wall the 24h notices came from.
    #
    # WHY THIS IS A DIGEST LINE AND NOT A CALENDAR REMINDER. The wave shipped a 100/UTC-day cap
    # whose impact was to be re-measured "in ~30 days". A reminder to go and look is prose
    # addressed to whoever happens to read it — the exact control this wave spent three chapters
    # retiring. Surfacing the split here makes a daily wall visible ON THE DAY IT FIRES, so the
    # question answers itself continuously instead of once, late, if someone remembers.
    #
    # It is also the ONLY way to see it: `alerts_fired` is written only on the DELIVERED path, so
    # once the cap ships that table is censored at the cap by construction and can never show a
    # refusal. `quota_notices_fired.limit_kind` is the sole durable record.
    #
    # DEFAULTED, and last: every field above `generated_at` lacks a default, so a non-defaulted
    # field could not be added after it. Defaults also keep every existing positional
    # construction in the suite working.
    #
    # Deliberately NOT added to the `bot_daily_metrics` Postgres row: that is a shared-schema
    # change on signal-MCP's database, which is a migration and a different wave. The operator's
    # TG digest is where this belongs today.
    quota_notices_monthly_24h: int = 0
    quota_notices_daily_24h: int = 0
    #: Rows in the window predating the 2026-08-28 migration, so `limit_kind IS NULL`. Shown only
    #: when non-zero, and only so the sub-counts always sum to the total — a breakdown that does
    #: not add up reads as a bug in the digest rather than as honest missing provenance.
    quota_notices_unclassified_24h: int = 0
    # GROWTH-TG-STARS-DEMAND-PROBE-W1 R4 — the demand probe. Defaulted and last, for the same
    # reason the three above are: every field before `generated_at` lacks a default, and the
    # suite constructs `DigestMetrics` positionally in places.
    #
    # Deliberately NOT added to the `bot_daily_metrics` Postgres row — that is a shared-schema
    # change on signal-MCP's database, i.e. a migration and a different wave. The same ruling the
    # quota-notice breakdown above got, for the same reason.
    stars_interest_users_30d: int = 0
    stars_interest_taps_30d: int = 0
    #: Distinct users whose LAST tap landed in the trailing day — the `+N` in the line. It is a
    #: movement figure, not a subset total: a user who tapped 20 days ago and again today counts
    #: here and in the 30d figure, which is correct, because both answer "is this live demand".
    stars_interest_users_24h: int = 0


# ── GROWTH-TG-STARS-DEMAND-PROBE-W1 R4: the demand probe's readout ───────────────────────────
#
# 🛑 THIS CONSTANT IS THE CONTRACT the deferred `GROWTH-TG-STARS-CHECKOUT-W1` reads, and it is
# cited in `research/payments-telegram-stars-usdt-research-2026-09-06.md` §Decision. It is a NAMED
# constant rather than a literal inside the f-string for the obvious reason and one less obvious
# one: a threshold nobody can grep is a threshold that gets "adjusted" in a hotfix, and the whole
# value of a pre-registered trigger is that it was chosen BEFORE the data arrived.
STARS_PROBE_TRIGGER_USERS: Final = 10
#: The demand window. 30 days, matching the free meter's own rolling window, so "current demand"
#: means the same span everywhere in this bot.
STARS_PROBE_WINDOW_DAYS: Final = 30


def _stars_interest_line(m: DigestMetrics) -> str:
    """The probe's one line. GROWTH-TG-STARS-DEMAND-PROBE-W1 R4.

    🛑 IT RENDERS AT ZERO. An omitted line is indistinguishable from a broken probe, a dead
    handler or a button that never shipped — and a demand measurement that can silently report
    nothing is worth less than no measurement, because absence reads as "no demand". The
    `🚫 Blocked the bot` line one screen up is conditional for a good reason (a count of zero
    there is genuinely uninteresting); this one is not, and the difference is that this number
    is the input to a DECISION.

    The trigger is evaluated HERE, by the digest, rather than by a cron or an alert: it is a
    daily readout for an operator, not operator-action drift, and the alert contract reserves
    Telegram for the latter.
    """
    line = (
        f"⭐ Stars interest: {m.stars_interest_users_30d} users"
        f" · {m.stars_interest_taps_30d} taps"
        f" (24h: +{m.stars_interest_users_24h})"
    )
    if m.stars_interest_users_30d >= STARS_PROBE_TRIGGER_USERS:
        line += " → STARS_PROBE_TRIGGER=FIRED"
    return line


def _link_state_suffix(m: DigestMetrics) -> str:
    """The linked cohort by state, rendered ONLY when something is not ENTITLED.

    A quiet line on a healthy day and a loud one on the day it matters — the same reasoning as
    the daily-wall split above. `unobserved` is rendered as its own bucket, never folded into a
    state: a mirror that has never been written is not evidence of entitlement.
    """
    by = m.linked_by_state or {}
    notable = {k: v for k, v in sorted(by.items()) if k != "ENTITLED" and v}
    if not notable:
        return ""
    detail = " · ".join(f"{k} {v}" for k, v in notable.items())
    return f"  (linked: ENTITLED {by.get('ENTITLED', 0)} · {detail})"


def _notice_split(m: DigestMetrics) -> str:
    """`🗓 Monthly N · ⏰ Daily N` — and `· ❔ Unclassified N` only when non-zero.

    The clock emoji is deliberate: the daily wall resets at 00:00 UTC, which is what the refusal
    copy tells the user, and the monthly one rolls on each subscriber's own 30-day window.
    """
    parts = [
        f"🗓 Monthly {m.quota_notices_monthly_24h}",
        f"⏰ Daily {m.quota_notices_daily_24h}",
    ]
    if m.quota_notices_unclassified_24h:
        parts.append(f"❔ Unclassified {m.quota_notices_unclassified_24h}")
    return " · ".join(parts)


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
        # GROWTH-TG-QUOTA-PARITY-W1 follow-up 2026-08-28: broken out by WHICH wall fired.
        # ONE grouped read rather than three counts — the total is derived from the parts, so
        # they cannot disagree with it.
        cur.execute(
            "SELECT limit_kind, COUNT(*) FROM quota_notices_fired "
            "WHERE fired_at >= datetime('now', '-1 day') GROUP BY limit_kind"
        )
        by_kind = {row[0]: int(row[1]) for row in cur.fetchall()}
        quota_notices_monthly_24h = by_kind.get("monthly", 0)
        quota_notices_daily_24h = by_kind.get("daily", 0)
        # NULL = written before the migration. These age out of the 24h window on their own.
        quota_notices_unclassified_24h = sum(
            n for k, n in by_kind.items() if k not in ("monthly", "daily")
        )
        quota_notices_24h = (
            quota_notices_monthly_24h
            + quota_notices_daily_24h
            + quota_notices_unclassified_24h
        )

        # GROWTH-TG-STARS-DEMAND-PROBE-W1 R4. Read through `count_interest`, which owns the
        # windowing rule (on `last_at`, so re-engaged demand stays current) and returns (0, 0)
        # rather than raising on a DB predating the migration — so the line renders on day one.
        # `now` is the snapshot's ONE clock read — the same instant `generated_at` is stamped
        # from — so the 30d and 24h windows cannot straddle a second and disagree.
        stars_interest_users_30d, stars_interest_taps_30d = db.count_interest(
            STARS_INTEREST_KIND,
            (now - timedelta(days=STARS_PROBE_WINDOW_DAYS)).isoformat(),
        )
        stars_interest_users_24h, _ = db.count_interest(
            STARS_INTEREST_KIND, (now - timedelta(days=1)).isoformat()
        )

    # BOT-QUOTA-REFUSAL-SEAM-W1: derived by projecting every reachable subscriber
    # through `evaluate_delivery` — the SAME decision the seam enforces. Never a
    # re-implemented `alert_count >= 100` in SQL, which would be a second derivation
    # of the very thing this wave exists to make single.
    walled_now, walled_silent, walled_paid = count_walled_now(db)
    plan_units_debited = db.count_plan_units_debited_last_24h()
    outbox_pending = db.count_pending_entitlement_debits()
    unmetered_24h = db.count_unmetered_deliveries_last_24h()
    linked_by_state = db.count_linked_by_entitlement_state()
    deployed_sha = read_deployed_sha()

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
        quota_notices_monthly_24h=quota_notices_monthly_24h,
        quota_notices_daily_24h=quota_notices_daily_24h,
        quota_notices_unclassified_24h=quota_notices_unclassified_24h,
        stars_interest_users_30d=stars_interest_users_30d,
        stars_interest_taps_30d=stars_interest_taps_30d,
        stars_interest_users_24h=stars_interest_users_24h,
        walled_now=walled_now,
        walled_silent=walled_silent,
        walled_paid=walled_paid,
        calls_paid_linked=calls_paid_linked,
        plan_units_debited=plan_units_debited,
        outbox_pending=outbox_pending,
        unmetered_24h=unmetered_24h,
        linked_by_state=linked_by_state,
        deployed_sha=deployed_sha,
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
        f"  🔒 Quota-exhausted notices: {m.quota_notices_24h}"
        f"  ({_notice_split(m)})",
        f"  💳 Plan debits 24h: {m.plan_units_debited}  (⏳ {m.outbox_pending} queued)",
        f"  🩸 Unmetered 24h: {m.unmetered_24h}{_link_state_suffix(m)}",
        f"  🚧 Walled now: {m.walled_now}"
        f"  (notified {m.walled_now - m.walled_silent} · silent {m.walled_silent})",
        "",
        _stars_interest_line(m),
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
   plan_units_debited, outbox_pending, walled_paid_now, deployed_sha, generated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
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
  deployed_sha=EXCLUDED.deployed_sha,
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
        m.deployed_sha,
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
