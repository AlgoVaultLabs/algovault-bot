"""SQLite persistence for algovault-bot.

C2 schema — subscribers + watchlists with TF-aware fields for the C3 lazy
dispatcher. WAL mode + synchronous=NORMAL for safe concurrent reads from the
C3 cron service while the long-lived bot service writes.

D1-C: per-user virtual-API-key columns dropped — the bot uses ONE shared
internal-bypass key against signal-MCP and tracks user quota in its own
``subscribers.alert_count`` counter. Quota window is the calendar month.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Iterator

from .dispatch_schedule import is_due


def _iso_to_epoch(value: object) -> int | None:
    """`last_fetched_at` is stored as an ISO datetime string. Returns None for NULL or for
    anything unparseable — callers treat None as never-fetched, so a corrupt stamp can never
    strand a row permanently un-dispatched."""
    if value is None:
        return None
    try:
        return int(datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


log = logging.getLogger(__name__)

DEFAULT_DB_PATH: Final = "/var/lib/algovault-bot/state.db"

PER_USER_WATCHLIST_CAP: Final = 50


SCHEMA_SQL: Final = """
CREATE TABLE IF NOT EXISTS subscribers (
  chat_id            INTEGER PRIMARY KEY,
  username           TEXT,
  lang_code          TEXT,
  alert_count        INTEGER NOT NULL DEFAULT 0,
  alerts_window_start TIMESTAMP,
  created_at         TIMESTAMP NOT NULL DEFAULT (datetime('now')),
  last_seen_at       TIMESTAMP NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watchlists (
  chat_id              INTEGER NOT NULL,
  coin                 TEXT NOT NULL,
  timeframe            TEXT NOT NULL,
  exchange             TEXT NOT NULL,
  alert_type           TEXT NOT NULL DEFAULT 'calls'
                         CHECK (alert_type IN ('regime', 'calls', 'both')),
  regime_last_seen     TEXT,
  regime_last_changed_at TIMESTAMP,
  regime_pending       TEXT,
  last_fetched_at      TIMESTAMP,
  last_verdict         TEXT,
  last_verdict_streak  INTEGER NOT NULL DEFAULT 0,
  added_at             TIMESTAMP NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (chat_id, coin, timeframe, exchange),
  FOREIGN KEY (chat_id) REFERENCES subscribers(chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_watchlists_due
  ON watchlists(timeframe, last_fetched_at);
CREATE INDEX IF NOT EXISTS idx_watchlists_chat ON watchlists(chat_id);
"""

# C4 migrations — additive columns for the regime-alert counter (drives the
# soft CTA on alert #1, 3, 7, 15, then every 10) and the admin /stats command.
# C5 will add 24h rate-limit window columns; we leave space for those by NOT
# putting them in the canonical SCHEMA_SQL (a wave's schema columns belong to
# the wave that introduced them).
C4_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN total_regime_alerts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscribers ADD COLUMN total_call_alerts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscribers ADD COLUMN total_ctas_shown INTEGER NOT NULL DEFAULT 0",
)

# C5 — anti-abuse 24h counters + quota-burn suppression timestamp.
# These fields drive the per-user rate limit (20 regime + 30 calls per 24h)
# and the 50-call/24h quota-burn protection (suppressed-until timestamp).
# As of 2026-05-08 the 24h caps were removed (Telegram doesn't impose any),
# but the columns stay for backward-compat — no code reads/writes them.
C5_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN alerts_24h_regime_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscribers ADD COLUMN alerts_24h_calls_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscribers ADD COLUMN alerts_24h_window_start TIMESTAMP",
    "ALTER TABLE subscribers ADD COLUMN calls_burn_suppressed_until TIMESTAMP",
)

# BOT-W2 C2 — Telegram bot per-user attribution via /start auth_<api_key>
# deep-link. The bot validates the api_key against signal-MCP's
# /api/bot/validate-key (internal-bypass-gated) and stores the linked tier
# here so the C3 quota gate can honor it.
W2_LINKED_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN linked_api_key TEXT",
    "ALTER TABLE subscribers ADD COLUMN linked_tier TEXT",
    "ALTER TABLE subscribers ADD COLUMN linked_at TIMESTAMP",
)

# BOT-ALERT-CLEANUP-W1 (2026-05-08) — per-threshold last-fired timestamps so
# the soft 75% / urgent 90% trade-call CTAs can be throttled to once per 24h
# per threshold per user. cta.py reads these + ``_now`` to decide whether
# to render the CTA snippet; alert_engine writes via ``mark_quota_cta_fired``
# after a successful Telegram push.
ALERT_CLEANUP_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN quota_75_last_fired_at TIMESTAMP",
    "ALTER TABLE subscribers ADD COLUMN quota_90_last_fired_at TIMESTAMP",
)

# BOT-ZOMBIE-W1 (2026-05-17) — mark subscribers who've blocked the bot so the
# digest/stats counts reflect reachable users only. Set by the alert engine
# when bot.send_message / send_photo raises Forbidden ("bot was blocked by
# the user"); cleared by handle_start when the subscriber sends /start
# again (i.e. they've unblocked).
ZOMBIE_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN bot_blocked_at TIMESTAMP",
)

# BOT-DIGEST-LAST24H-W1 (2026-05-21) — per-alert log so the daily digest can
# report a rolling-24h window instead of the lifetime sum of
# subscribers.total_regime_alerts + total_call_alerts. The lifetime counters
# stay in place (admin /stats still reports them under a separate header),
# but the daily digest now answers "what happened yesterday" via a
# SELECT COUNT(*) ... WHERE fired_at >= datetime('now', '-1 day') filter
# against this table.
#
# Idempotent CREATE pattern: SQLite has CREATE TABLE IF NOT EXISTS so no
# try/except needed here, but we keep the ADD-COLUMN-style tuple shape for
# consistency with the rest of the wave migrations + so a later wave can
# tack on indexes via the same loop.
DIGEST_LAST24H_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS alerts_fired ("
    "  id       INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  chat_id  INTEGER NOT NULL,"
    "  kind     TEXT NOT NULL CHECK (kind IN ('regime', 'call')),"
    "  fired_at TIMESTAMP NOT NULL DEFAULT (datetime('now'))"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_alerts_fired_at ON alerts_fired(fired_at)",
    "CREATE INDEX IF NOT EXISTS idx_alerts_fired_kind_at ON alerts_fired(kind, fired_at)",
)

# BOT-DIGEST-COUNT-ALL-CALLS-W1 (2026-06-25) — discriminate which delivery path wrote
# each alerts_fired row so the daily digest can break out 📈 Calls by source (watch push
# vs scanwatch digest vs on-demand scan). DEFAULT 'watch' backfills every historical row
# to the only path that ever wrote alerts_fired before this wave — no separate UPDATE.
# SQLite has no ADD COLUMN IF NOT EXISTS, so the shared _init_schema try/except (swallows
# "duplicate column name") makes this idempotent on every init.
DIGEST_SOURCE_MIGRATIONS = (
    "ALTER TABLE alerts_fired ADD COLUMN source TEXT NOT NULL DEFAULT 'watch'",
)

# Allowed alerts_fired.source values (BOT-DIGEST-COUNT-ALL-CALLS-W1). 'watch'|'scanwatch'|
# 'scan' are wired this wave; 'webhook'|'batch' are reserved for forward delivery paths
# (webhook top:N, batch tools) so they record correctly the day they ship.
ALLOWED_ALERT_SOURCES = frozenset({"watch", "scanwatch", "scan", "webhook", "batch"})

# BOT-DIGEST-QUOTA-NOTICES-W1 (2026-06-15) — per-notice log for quota-exhausted
# trade-call notices, so the daily digest + admin /stats can surface a rolling
# 24h count of "BUY/SELL signals a watcher would have received but for the
# 100/mo free-quota cap". Deliberately a SEPARATE table from alerts_fired:
# alerts_fired tracks delivered signal volume (regime shifts + trade-call
# cards), whereas a quota-exhausted notice is an operator-UX nudge, not signal
# volume. Keeping it separate also freezes the alerts_fired
# CHECK(kind IN ('regime','call')) contract — no table rebuild, existing alert
# rows untouched. CREATE TABLE IF NOT EXISTS is idempotent on every init.
QUOTA_NOTICES_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS quota_notices_fired ("
    "  id       INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  chat_id  INTEGER NOT NULL,"
    "  fired_at TIMESTAMP NOT NULL DEFAULT (datetime('now'))"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_quota_notices_fired_at "
    "ON quota_notices_fired(fired_at)",
)

# ACTIVATION-FUNNEL-AUDIT-W1 (2026-05-28): tracks the first non-/start command
# per subscriber for the `tg_bot_first_command` funnel stage. NULL = subscriber
# hasn't issued any non-/start command yet. Set once via
# `set_first_command_fired_at()` on the first wrapper invocation that detects
# the NULL value; never reset. Snapshot reader (scripts/funnel-snapshot.ts)
# grabs the COUNT from the alerts.log JSON-line stream rather than directly
# from this column — the column is a per-subscriber DEDUP flag for the
# `log_alert_event("tg_bot_first_command", ...)` emit, not the canonical
# count source.
ACTIVATION_FUNNEL_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN first_command_fired_at TIMESTAMP",
)

# TG-BROADCAST-STACK-W1 C1 (2026-05-28): NEW tg_broadcasts ledger table for
# idempotent broadcast fanout via src/algovault_bot/broadcast.py. Per CH1
# spec: event_id PK = "<broadcast_type>:<YYYY-MM-DD>:<body_hash[:8]>";
# re-firing within the same day with the same body is suppressed at the
# iterate-start boundary; cleanup is operator-discretionary (retention is
# the ledger of WHEN + WHAT fired). Idempotent via CREATE TABLE IF NOT
# EXISTS — fresh installs and existing deployments converge cleanly.
BROADCASTS_TABLE_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS tg_broadcasts ("
    "  event_id        TEXT PRIMARY KEY,"
    "  broadcast_type  TEXT NOT NULL,"
    "  body_hash       TEXT NOT NULL,"
    "  sent_count      INTEGER NOT NULL DEFAULT 0,"
    "  skipped_count   INTEGER NOT NULL DEFAULT 0,"
    "  failed_count    INTEGER NOT NULL DEFAULT 0,"
    "  fired_at        TIMESTAMP NOT NULL DEFAULT (datetime('now'))"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_tg_broadcasts_fired_at ON tg_broadcasts(fired_at)",
    "CREATE INDEX IF NOT EXISTS idx_tg_broadcasts_type_fired ON tg_broadcasts(broadcast_type, fired_at)",
)

# TG-BROADCAST-STACK-W1 C3 (2026-05-28): paywall-at-quota dedup flags.
# Three columns track WHEN each threshold first fired DM to subscriber
# (per-month idempotency; resets via separate monthly-rollover process).
# NULL means threshold has never fired for this subscriber.
PAYWALL_HOOK_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN quota_hit_soft_at TIMESTAMP",
    "ALTER TABLE subscribers ADD COLUMN quota_hit_hard_at TIMESTAMP",
    "ALTER TABLE subscribers ADD COLUMN quota_hit_block_at TIMESTAMP",
)

# BOT-QUOTA-REFUSAL-SEAM-W1 (2026-08-16): the wall's own notice stamp. Completes
# the quota_<threshold>_last_fired_at family (75 / 90 existed; 100 did not, which
# is why ``mark_quota_cta_fired('100', …)`` was a silent no-op). Compared against
# ``alerts_window_start`` so the notice re-arms once per exhaustion window with no
# timer. NULL = this subscriber has never been told they hit the wall.
QUOTA_WALL_NOTICE_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN quota_100_last_fired_at TIMESTAMP",
)

# PRICING-BOT-DELIVERY-METERING-W1 CH4a — the PLAN MIRROR.
#
# A paid-linked subscriber's allowance lives on signal-MCP (`quota_usage`), reachable only over
# HTTP. `evaluate_delivery` runs O(subscribers)/minute on the dispatch loop and is contractually a
# PURE LOCAL READ — so the wall cannot ask the network. These columns are a local copy of the
# server's answer, refreshed by the drainer whenever it debits or polls.
#
# They live ON `subscribers` rather than in a side table for one reason: `get_subscriber` already
# returns the whole row, so the mirror costs ZERO extra queries in the hot path.
#
# `plan_state_as_of IS NULL` means NEVER OBSERVED, which is distinct from "observed as zero" — CH5
# serves on it rather than walling, because you must never wall a paying customer on a measurement
# you could not take.
PLAN_MIRROR_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN plan_used INTEGER",
    "ALTER TABLE subscribers ADD COLUMN plan_total INTEGER",          # NULL = no ceiling
    "ALTER TABLE subscribers ADD COLUMN plan_allowed INTEGER",        # 0/1
    "ALTER TABLE subscribers ADD COLUMN plan_limit_kind TEXT",        # 'monthly' | 'daily' | NULL
    "ALTER TABLE subscribers ADD COLUMN plan_period_start TEXT",      # monthly episode key
    "ALTER TABLE subscribers ADD COLUMN plan_daily_day TEXT",         # daily episode key
    "ALTER TABLE subscribers ADD COLUMN plan_next_json TEXT",         # verbatim next_plan JSON
    "ALTER TABLE subscribers ADD COLUMN plan_state_as_of TIMESTAMP",  # NULL = never observed
    "ALTER TABLE subscribers ADD COLUMN plan_state_source TEXT",      # 'debit' | 'poll'
    "ALTER TABLE subscribers ADD COLUMN plan_wall_notice_day TEXT",   # UTC date of last DAILY notice
)

# OPS-VALIDATE-KEY-INDETERMINATE-W1 CH4/CH6 — THE MIRROR CARRIES THE ENTITLEMENT STATE.
#
# The server has answered four distinct states since CH2 (ENTITLED / DUNNING / NOT_ENTITLED /
# INDETERMINATE) and the bot stored none of them, so "this subscriber is being served while
# Stripe dunns them" existed only as a log line in a file with no reader. That is exactly how
# 1,987 uncharged debits and 2,025 delivered alerts accumulated for nine days unnoticed.
#
# 🛑 NO SECOND FRESHNESS CLOCK, for the same reason `plan_tier` has none: this column is stamped
# by the EXISTING `plan_state_as_of` in the same `update_plan_mirror` write. A column with its own
# timestamp is a second clock to drift.
#
# NULL = never observed, and it MUST read as unobserved rather than as any state — a mirror
# written by a server predating CH2 carries no state, and defaulting that to ENTITLED would grant
# and to NOT_ENTITLED would revoke.
ENTITLEMENT_STATE_MIRROR_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN plan_entitlement_state TEXT",
)

# OPS-BOT-LINKED-TIER-REFRESH-W1 CH2 — THE MIRROR CARRIES TIER.
#
# `linked_tier` was written once at /link and never re-read, while the server's CURRENT tier
# arrived in every entitlement response and was thrown away. Two copies of one fact, and the stale
# copy was the one every label read: chat 1061466212 upgraded to `pro` on the server, kept
# `linked_tier='starter'` in the bot, and was shown "Starter plan" on every trade-call card while
# its debits correctly charged the Pro allowance — the mirror already carried server truth for the
# FIGURES and `linked_tier` never got the same discipline.
#
# 🛑 NO SECOND FRESHNESS CLOCK. This column is stamped by the EXISTING `plan_state_as_of` /
# `plan_state_source`, because one `as_of` for the whole mirror row cannot disagree with itself
# about how fresh a single response was, and two of them eventually would.
#
# NULL = UNOBSERVED, which is distinct from "observed as free". `quota.effective_tier` falls back
# to `linked_tier` on NULL or stale, so the floor of this change is exactly the prior behaviour.
LINKED_TIER_MIRROR_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN plan_tier TEXT",  # NULL = unobserved
)

# OPS-BOT-LINKED-TIER-REFRESH-W1 CH3 — THE LINK GETS A LIFECYCLE.
#
# Until now a link had creation and no other state: nothing ever re-asked the server, so a
# revoked key kept paid treatment forever. Chat 1793689937 has been in exactly that state
# since 2026-05-08 — `linked_tier='starter'`, `validate-key` answering 404 — and because
# `linked_tier in PAID_TIERS` makes `consume_quota` a no-op, the bot-side 100/mo wall never
# applied to them. Revenue leakage with no detector.
#
# The counter measures SUSTAINED DETERMINED INVALIDITY and nothing else:
#   `link_invalid_streak`  consecutive determined-INVALID observations. ANY `VALID` or
#                          `INDETERMINATE` resets it to 0.
#   `link_invalid_since`   when the CURRENT streak began. NULL whenever the streak is 0.
#   `link_downgrade_notice_at`  the episode stamp for 3d's notice — one per downgrade.
#
# 🛑 `link_invalid_streak` IS NOT A TIMER. The grace window is measured from
# `link_invalid_since`, not from the count, because the drain cadence is 10 fires/hour with
# a 16-minute gap at the top of the hour — a count would silently mean different amounts of
# wall-clock depending on where in the hour the streak started.
LINK_LIFECYCLE_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN link_invalid_since TIMESTAMP",
    "ALTER TABLE subscribers ADD COLUMN link_invalid_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscribers ADD COLUMN link_downgrade_notice_at TIMESTAMP",
)

# GROWTH-TG-QUOTA-PARITY-W1 CH2 (2026-08-27) — the DAILY free meter + the LADDER MIRROR.
#
# A NEW tuple, never an edit to an existing one: the tuples above are applied to live databases
# and editing one changes what a replayed migration means on a box that already ran it.
#
# ── The three subscriber columns (2c) ────────────────────────────────────────────────────────
# The free lane metered MONTHLY only. It now meters monthly AND daily, and a call is refused when
# EITHER is exhausted — two real caps, not a cap and a sub-limit.
#   `alerts_day_count`      alerts delivered in the CURRENT UTC day
#   `alerts_day`            that day's UTC key, 'YYYY-MM-DD'. A mismatch IS the roll signal, so
#                           there is no reset job and no timer — the same self-cleaning shape the
#                           paid lane's `plan_daily_day` already uses.
#   `quota_day_notice_day`  UTC date of the last DAILY-wall notice.
#
# 🛑 `quota_day_notice_day` MUST NOT be collapsed into `quota_100_last_fired_at`. The monthly stamp
# is scoped to a 30-day window; the daily wall re-arms every UTC day. Reusing it would announce the
# daily wall at most ONCE EVER — the user hits it again on day 2 and hears nothing, which is the
# silent-refusal population BOT-QUOTA-REFUSAL-SEAM-W1 found refused ~10,000 times.
#
# ── The ladder mirror (2a + 3b-2) ────────────────────────────────────────────────────────────
# A single row holding signal-MCP's published ladder, refreshed by the EXISTING entitlement drain.
# It carries the free rung AND the starter rung because BOTH are hand-typed in shipped copy today
# and both arrive in the SAME response — one read, one table, one fallback path. Shipping the free
# rung now and widening for the price later would be two migrations for one need.
#
# `CHECK (id = 1)` is the single-row guard. Without it a retried write can mint a second row and
# the read silently becomes ordering-dependent — a load-bearing property rented from SQLite's row
# order, which is exactly what `build-and-runtime.md` forbids.
# GROWTH-TG-QUOTA-PARITY-W1 FOLLOW-UP (2026-08-27) — WHICH WALL FIRED.
#
# 🛑 WITHOUT THIS COLUMN THE DAILY CAP IS UNMEASURABLE, and the +30d impact wave
# (`GROWTH-TG-DAILY-CAP-IMPACT-W1`) cannot answer its own question.
#
# The reason is a censoring effect, not an oversight in the analysis:
#   • CH0/P5 measured "does the daily cap bind?" from `alerts_fired` — max 74 alerts/UTC-day
#     over 298 free chat-days, zero days above 100.
#   • `record_call_delivered` writes `alerts_fired` ONLY on the delivered path, AFTER the quota
#     gate allows. A REFUSED alert never lands a row.
#   • So the moment the 100/day cap ships, `alerts_fired` is capped at 100/day BY CONSTRUCTION.
#     Re-running the exact CH0 query at +30d returns the same frozen pre-cap rows whether the
#     cap bound zero times or five hundred — a confident ZERO from an instrument structurally
#     incapable of seeing the thing (`verification-gates.md`, the OPS-CF-ORIGIN-LOCK-W1 sign).
#
# `quota_notices_fired` is the one table the REFUSAL path writes. It carried
# `(id, chat_id, fired_at)` and could not tell a monthly wall from a daily one. `limit_kind`
# makes it the durable per-episode record the impact wave needs.
#
# NULL is honest and expected: every row written before this migration predates the daily cap
# and is therefore monthly by construction. Backfilling them to 'monthly' would be inventing
# a measurement nobody took — an absent value is not a zero.
QUOTA_NOTICE_LIMIT_KIND_MIGRATIONS = (
    "ALTER TABLE quota_notices_fired ADD COLUMN limit_kind TEXT",
)

# OPS-BOT-DISPATCH-LATENCY-W1 CH1 — BOUNDED RETRY FOR A FAILED FETCH.
#
# `update_watch_after_fetch` sat at function-body indent in `process_one_row`, OUTSIDE both
# `except McpError` blocks, so a failed MCP call still stamped `last_fetched_at` and advanced
# the dispatch bucket. `is_due` is a strict bucket comparison, so that bar's alert was never
# retried and never delivered — silently, with only a `warning` line. Measured: 150 MCP
# failures over the 40-day journal, and on a 4h row one of them costs a 4-hour blind spot.
#
# Simply NOT stamping is the wrong fix and would have been worse: the row then stays due on
# EVERY tick for the rest of its bucket — up to 240 retries on a 4h row against the venue and
# the quota meter. So the stamp is SPLIT instead:
#   - what we LEARNED (last_verdict / streak / regime_last_seen) persists either way; the
#     regime lane's own comment already establishes that flap-suppression state must survive
#     a non-delivery, and a failed fetch is the same case.
#   - whether the bucket was SERVICED is now conditional, bounded by this counter.
FETCH_RETRY_MIGRATIONS = (
    "ALTER TABLE watchlists ADD COLUMN fetch_fail_streak INTEGER NOT NULL DEFAULT 0",
)

# Attempts per bucket before the row gives up and lets the bucket advance. 3 is one original
# plus two retries, i.e. ~2 extra minutes of recovery on the 60s tick — comfortably inside the
# shortest schedulable timeframe (3m; 1m is excluded from PUSH_TIMEFRAMES) so a retry can never
# leak into the following bar even at the fastest cadence.
MAX_FETCH_ATTEMPTS_PER_BUCKET: Final = 3

QUOTA_PARITY_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN alerts_day_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscribers ADD COLUMN alerts_day TEXT",
    "ALTER TABLE subscribers ADD COLUMN quota_day_notice_day TEXT",
    "CREATE TABLE IF NOT EXISTS free_tier_ladder ("
    "  id                    INTEGER PRIMARY KEY CHECK (id = 1),"
    "  free_monthly          INTEGER,"
    "  free_daily            INTEGER,"
    "  starter_price_usd     REAL,"
    "  starter_monthly_calls INTEGER,"
    "  fetched_at            TIMESTAMP"
    ")",
)

# GROWTH-TG-PLAN-PICKER-W1 R2 — the mirror widens from the free + starter rungs to the whole
# four-SKU ladder the plan picker renders (starter/pro x month/6month, plus both daily caps).
#
# ALL NULLABLE, deliberately. A drain that runs against a signal-MCP which has not yet deployed
# `price_usd_6month` writes NULL here, and `quota.resolve_ladder` reads NULL as "absent" and
# serves its pinned constant. That is what makes the deploy ORDER of the two repos free rather
# than lockstep — and a NOT NULL column without a default would take the serving path down for
# the whole window between the two deploys.
#
# A separate tuple rather than more lines in QUOTA_PARITY_MIGRATIONS: these run against a table
# the previous tuple CREATES, so the ordering is a real dependency, and a tuple named for its
# wave is how the next reader can tell which columns arrived together.
PLAN_PICKER_MIGRATIONS = (
    "ALTER TABLE free_tier_ladder ADD COLUMN starter_daily_calls INTEGER",
    "ALTER TABLE free_tier_ladder ADD COLUMN starter_price_usd_6month REAL",
    "ALTER TABLE free_tier_ladder ADD COLUMN pro_price_usd REAL",
    "ALTER TABLE free_tier_ladder ADD COLUMN pro_monthly_calls INTEGER",
    "ALTER TABLE free_tier_ladder ADD COLUMN pro_daily_calls INTEGER",
    "ALTER TABLE free_tier_ladder ADD COLUMN pro_price_usd_6month REAL",
)

# PRICING-BOT-DELIVERY-METERING-W1 CH4a — the DEBIT OUTBOX.
#
# A delivery must never be blocked, delayed or lost by a metering call. The recorder enqueues here
# (local SQLite, same autocommitting path as the alerts_fired INSERT it sits beside — it cannot
# fail the delivery), and a cron drainer POSTs to the server out of band.
#
# 🛑 `api_key` is deliberately NOT a column. The drainer reads `subscribers.linked_api_key` at SEND
# time. A key copied into a queue row would survive an unlink and charge a revoked key — the row
# stores `chat_id`, an identity to resolve, never a credential to replay.
#
# `idem_key` is UNIQUE: it is `bot:<chat_id>:<alerts_fired.id>`, so the delivery ledger IS the
# idempotency source. No clock, no UUID, no counter.
ENTITLEMENT_OUTBOX_MIGRATIONS = (
    """CREATE TABLE IF NOT EXISTS entitlement_outbox (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      idem_key   TEXT NOT NULL UNIQUE,
      chat_id    INTEGER NOT NULL,
      channel    TEXT NOT NULL,
      kind       TEXT NOT NULL CHECK (kind IN ('regime','call')),
      units      INTEGER NOT NULL,
      created_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
      sent_at    TIMESTAMP,
      attempts   INTEGER NOT NULL DEFAULT 0,
      last_error TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_entitlement_outbox_pending ON entitlement_outbox(sent_at, id)",
)

# TG-BROADCAST-STACK-W1 C4 (2026-05-28): viral /unlock_premium_alerts state
# machine. unlock_status enum: 'not_started' | 'pending_x_screenshot' |
# 'pending_npm_call' | 'verified' | 'expired'. unlock_method: 'x_follow'
# | 'npm_install'. NULL on all 4 columns until first /unlock attempt.
UNLOCK_STATE_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN unlock_status TEXT",
    "ALTER TABLE subscribers ADD COLUMN unlock_verified_at TIMESTAMP",
    "ALTER TABLE subscribers ADD COLUMN unlock_method TEXT",
    "ALTER TABLE subscribers ADD COLUMN unlock_screenshot_path TEXT",
    # REFERRAL-PARITY-NOTIFS-W1 / C2: cache the engine's referral CODE for this
    # chat (set when the user runs /referral) so the notification drain can map a
    # pending TG row (keyed by code) → chat_id locally — no engine chat_id, no
    # cross-repo HMAC.
    "ALTER TABLE subscribers ADD COLUMN referral_code TEXT",
)

# TG-BROADCAST-STACK-W1 C4 (2026-05-28): NEW tg_pro_grants table — separate
# from real Stripe Pro tier (this is a 30-day GRANT not a CHARGE).
# chat_id PK = one active grant per subscriber at a time; insert-or-replace
# semantics on re-grant (e.g. extended via another verified action).
PRO_GRANTS_TABLE_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS tg_pro_grants ("
    "  chat_id     INTEGER PRIMARY KEY,"
    "  granted_at  TIMESTAMP NOT NULL DEFAULT (datetime('now')),"
    "  expires_at  TIMESTAMP NOT NULL,"
    "  method      TEXT NOT NULL"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_tg_pro_grants_expires_at ON tg_pro_grants(expires_at)",
)

# TG-BROADCAST-STACK-W1 C6 (2026-05-28): npm-install verification tracking.
# npm_unlock_session_id = UUIDv4 generated by bot when subscriber taps the
# [Install] inline button. npm_unlock_detected_at = when scripts/check-npm-
# unlocks.py first detected a matching funnel_events row.
NPM_UNLOCK_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN npm_unlock_session_id TEXT",
    "ALTER TABLE subscribers ADD COLUMN npm_unlock_detected_at TIMESTAMP",
)

# FEATURE-PARITY-CHANNELS-W1 CH4 (2026-06-08): scheduled scan-digest subscriptions —
# the TG-bot twin of the webhook scan_digest. One row per (chat, top_n, tf, exchange)
# scan filter; `cadence` is timeframe-derived by default (floor 1h); `last_fired_bucket`
# is the epoch of the last cadence bucket pushed → at most one digest per bucket.
# SCAN-RANKBY-W1 (2026-06-27): `rank_by` is part of the watch IDENTITY — a chat can
# hold both an `oi` and an `nfr` standing scan for the same (top_n,tf,exchange), so it
# joins the PRIMARY KEY. Fresh DBs get this shape directly; EXISTING DBs are migrated
# by the row-preserving table-recreate in `_migrate_scan_watches_rank_by` (idempotent).
SCAN_WATCHES_CREATE_SQL = (
    "CREATE TABLE IF NOT EXISTS scan_watches ("
    "  chat_id           INTEGER NOT NULL,"
    "  top_n             INTEGER NOT NULL DEFAULT 20,"
    "  timeframe         TEXT NOT NULL DEFAULT '15m',"
    "  exchange          TEXT NOT NULL DEFAULT 'BINANCE',"
    "  cadence           TEXT NOT NULL DEFAULT '1h' CHECK (cadence IN ('1h','4h','1d')),"
    "  last_fired_bucket INTEGER NOT NULL DEFAULT 0,"
    "  added_at          TIMESTAMP NOT NULL DEFAULT (datetime('now')),"
    "  rank_by           TEXT NOT NULL DEFAULT 'oi',"
    "  PRIMARY KEY (chat_id, top_n, timeframe, exchange, rank_by),"
    "  FOREIGN KEY (chat_id) REFERENCES subscribers(chat_id) ON DELETE CASCADE"
    ")"
)
SCAN_WATCHES_TABLE_MIGRATIONS = (
    SCAN_WATCHES_CREATE_SQL,
    "CREATE INDEX IF NOT EXISTS idx_scan_watches_chat ON scan_watches(chat_id)",
)

# TG-WATCH-ADOPTION-BROADCAST-W1 (2026-06-19): one-time first-watch onboarding
# nudge dedup flag. NULL = the 0-watch onboarding nudge has never been sent to
# this subscriber; an ISO timestamp = it fired once (never re-send — anti-spam).
# Set via ``mark_first_watch_nudge_sent``; the 0-engagement segment query
# (``list_zero_engagement_unnudged``) excludes any row where this is non-NULL.
FIRST_WATCH_NUDGE_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN first_watch_nudge_sent_at TIMESTAMP",
)

# TG-REFERRAL-W1 (C2): bot-side referee bonus-call pool. A user referred via a
# ?start=ref_<CODE> deep link gets +N here (N = the engine's SoT bonus_calls);
# consume_quota draws it AFTER the monthly free 100 (quota.py). Persistent —
# NOT reset on the 30-day window roll (mirrors the server's referral_bonus pool).
REFERRAL_BONUS_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN referral_bonus_remaining INTEGER NOT NULL DEFAULT 0",
)

# TG-REFERRAL-W1 (C3): value-moment referral-nudge throttle (≤1 per 7d per user;
# enforced in cta.referral_nudge_text). NULL until the first nudge fires.
REFERRAL_NUDGE_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN referral_nudge_last_at TIMESTAMP",
)

# TG-SCANWATCH-TF-CADENCE-W1 (2026-07-09): per-scanwatch "last-sent signature" for content
# dedup — a stable hash of the last-delivered actionable (coin:call) set. NULL = never sent.
# With TF-based re-scan cadence a persistent BUY would re-send each TF-bucket; comparing this
# sig makes the digest fire ONLY on a NEW/changed set (all-HOLD resets it to ''). Additive
# ADD COLUMN (no CHECK, no table-rebuild) — idempotent via the _init_schema try/except loop.
SCANWATCH_SIG_MIGRATIONS = (
    "ALTER TABLE scan_watches ADD COLUMN last_sent_sig TEXT",
)

# GROWTH-TG-CHANNEL-ACQUISITION-W1 (CH1): first-touch acquisition channel for a
# subscriber, set from a ``?start=src_<channel>`` deep link. NULL = joined before
# this wave, or via an untagged link — absence is absence, never a synthetic value.
#
# WHY THIS AND NOT signup_attribution: a /start payload arrives THROUGH Telegram, so
# the chat_id is authenticated by construction. The funnel's `utm_source` is a query
# string on a public URL — Step 0 measured 21 rows (12 tg_bot + 9 direct) minted by a
# single Baidu crawler replaying a discovered signup link, which is what produced the
# false "tg_bot converts 7.69%". This column is the trustworthy side of that pair.
#
# Additive ADD COLUMN (no CHECK, no table-rebuild) — idempotent via the _init_schema
# try/except loop, because SQLite has no ADD COLUMN IF NOT EXISTS.
ACQUISITION_SOURCE_MIGRATIONS = (
    "ALTER TABLE subscribers ADD COLUMN acquisition_source TEXT",
)

# Recorded when a payload carries a source we do not recognise. Default-DENY: an
# unknown tag is never trusted into a channel metric, but it is also never dropped —
# the count of `unknown` IS the attribution-coverage ceiling CH4 has to report.
UNKNOWN_ACQUISITION_SOURCE: Final = "unknown"

# The closed acquisition-channel vocabulary (mirrors adoption._VALID_SOURCES).
# Adding a channel = adding a row here; nothing else changes. Slugs are
# lowercase [a-z0-9_] and MUST NOT contain '-', which is the composite separator
# in the deep-link grammar (see handlers.parse_start_payload).
ACQUISITION_CHANNELS: Final = frozenset(
    {
        "x",             # X/Twitter posts + replies
        "devto",         # dev.to articles (canonical announcement channel)
        "github",        # repo READMEs / releases / issues
        "npm",           # npm package page
        "geo",           # AI-engine / GEO answer surfaces
        "awesome_list",  # crypto-vertical awesome-list entries
        "referral_card", # the in-bot referral share card
        "landing",       # algovault.com landing + docs
        "partner",       # partner / integration placements
    }
)


def normalize_acquisition_source(raw: str | None) -> str:
    """Default-deny a raw deep-link channel tag to a storable value.

    Anything not in ``ACQUISITION_CHANNELS`` becomes ``unknown`` — never the raw
    string, so a crafted payload cannot invent a channel in the readout. Returns
    ``unknown`` (not None) for empty/garbage: the user DID arrive via a tagged
    link we could not read, and that is a different fact from "no tag at all".
    """
    if not raw:
        return UNKNOWN_ACQUISITION_SOURCE
    slug = raw.strip().lower()
    if slug in ACQUISITION_CHANNELS:
        return slug
    return UNKNOWN_ACQUISITION_SOURCE


def _migrate_scan_watches_rank_by(cur: sqlite3.Cursor) -> None:
    """SCAN-RANKBY-W1: widen the scan_watches PRIMARY KEY to include ``rank_by`` on an
    EXISTING DB. SQLite can't ALTER a PK → recreate-and-copy. Guardrails (ratified):
      • IDEMPOTENT — PRAGMA table_info pre-check; no-op when rank_by already present
        (fresh DBs already have the new shape) or the table doesn't exist yet.
      • BACKED UP — snapshot `scan_watches_backup_rankby` before any structural change.
      • ATOMIC — the whole recreate runs in one BEGIN IMMEDIATE … COMMIT (ROLLBACK on error;
        the connection is autocommit, so the transaction is explicit).
      • ROW-PRESERVING — assert COUNT(*) pre == post; existing rows backfill rank_by='oi'
        (byte-unchanged behavior for current subscribers).
    quota.py / 100-mo / PAID_TIERS / units are untouched.
    """
    cols = [r[1] for r in cur.execute("PRAGMA table_info(scan_watches)").fetchall()]
    if not cols or "rank_by" in cols:
        return  # table absent (CREATE handles it) OR already migrated → no-op
    pre = cur.execute("SELECT COUNT(*) FROM scan_watches").fetchone()[0]
    # Official SQLite recreate procedure: foreign_keys OFF for the duration (cannot toggle
    # inside a txn), so an orphan row (chat_id not in subscribers — data drift) is PRESERVED
    # rather than crashing boot. The new table still DECLARES the FK for future writes.
    fk_was_on = bool(cur.execute("PRAGMA foreign_keys").fetchone()[0])
    if fk_was_on:
        cur.execute("PRAGMA foreign_keys=OFF")
    try:
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("DROP TABLE IF EXISTS scan_watches_backup_rankby")
            cur.execute("CREATE TABLE scan_watches_backup_rankby AS SELECT * FROM scan_watches")
            cur.execute("ALTER TABLE scan_watches RENAME TO scan_watches_pre_rankby")
            cur.execute(SCAN_WATCHES_CREATE_SQL)
            cur.execute(
                "INSERT INTO scan_watches "
                "(chat_id, top_n, timeframe, exchange, cadence, last_fired_bucket, added_at, rank_by) "
                "SELECT chat_id, top_n, timeframe, exchange, cadence, last_fired_bucket, added_at, 'oi' "
                "FROM scan_watches_pre_rankby"
            )
            post = cur.execute("SELECT COUNT(*) FROM scan_watches").fetchone()[0]
            if post != pre:
                raise RuntimeError(f"scan_watches rank_by migration row-count mismatch: pre={pre} post={post}")
            cur.execute("DROP TABLE scan_watches_pre_rankby")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_scan_watches_chat ON scan_watches(chat_id)")
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
    finally:
        if fk_was_on:
            cur.execute("PRAGMA foreign_keys=ON")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class Database:
    """Thin SQLite wrapper. Threadsafe via per-call connections."""

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = path
        self._ensure_parent_dir()
        self._init_schema()
        self._enforce_mode_660()

    def _ensure_parent_dir(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _init_schema(self) -> None:
        with self._cursor() as cur:
            cur.executescript(SCHEMA_SQL)
            # C4 additive migrations — idempotent via try/except on
            # "duplicate column name" (SQLite < 3.35 lacks ADD COLUMN IF NOT EXISTS).
            for stmt in (
                *C4_MIGRATIONS,
                *C5_MIGRATIONS,
                *W2_LINKED_MIGRATIONS,
                *ALERT_CLEANUP_MIGRATIONS,
                *ZOMBIE_MIGRATIONS,
                *DIGEST_LAST24H_MIGRATIONS,
                *QUOTA_NOTICES_MIGRATIONS,
                *ACTIVATION_FUNNEL_MIGRATIONS,
                # TG-BROADCAST-STACK-W1 (2026-05-28): broadcast ledger +
                # paywall flags + unlock state + pro-grants + npm-unlock cols.
                *BROADCASTS_TABLE_MIGRATIONS,
                *PAYWALL_HOOK_MIGRATIONS,
                *UNLOCK_STATE_MIGRATIONS,
                *PRO_GRANTS_TABLE_MIGRATIONS,
                *NPM_UNLOCK_MIGRATIONS,
                *SCAN_WATCHES_TABLE_MIGRATIONS,
                # TG-WATCH-ADOPTION-BROADCAST-W1 (2026-06-19): first-watch nudge flag.
                *FIRST_WATCH_NUDGE_MIGRATIONS,
                # TG-REFERRAL-W1 (2026-06-20): bot-side referee bonus-call pool.
                *REFERRAL_BONUS_MIGRATIONS,
                # TG-REFERRAL-W1 C3 (2026-06-20): value-moment nudge throttle.
                *REFERRAL_NUDGE_MIGRATIONS,
                # BOT-DIGEST-COUNT-ALL-CALLS-W1 (2026-06-25): alerts_fired.source
                # discriminator (runs after DIGEST_LAST24H_MIGRATIONS creates the table).
                *DIGEST_SOURCE_MIGRATIONS,
                # GROWTH-TG-CHANNEL-ACQUISITION-W1 (2026-08-05, CH1): first-touch
                # acquisition channel from ?start=src_<channel>.
                *ACQUISITION_SOURCE_MIGRATIONS,
                # BOT-QUOTA-REFUSAL-SEAM-W1 (2026-08-16): the wall's notice stamp.
                *QUOTA_WALL_NOTICE_MIGRATIONS,
                # PRICING-BOT-DELIVERY-METERING-W1 (2026-08-17): plan mirror + debit outbox.
                *PLAN_MIRROR_MIGRATIONS,
                *ENTITLEMENT_STATE_MIRROR_MIGRATIONS,
                *ENTITLEMENT_OUTBOX_MIGRATIONS,
                # OPS-BOT-LINKED-TIER-REFRESH-W1 (2026-08-21): server-authoritative tier,
                # stamped by the plan mirror's existing as_of.
                *LINKED_TIER_MIRROR_MIGRATIONS,
                *LINK_LIFECYCLE_MIGRATIONS,
                # GROWTH-TG-QUOTA-PARITY-W1 (2026-08-27): daily free meter + ladder mirror.
                *QUOTA_PARITY_MIGRATIONS,
                # GROWTH-TG-PLAN-PICKER-W1 (2026-09-06): the mirror carries all four SKUs.
                # MUST follow QUOTA_PARITY_MIGRATIONS — it ALTERs the table that one creates.
                *PLAN_PICKER_MIGRATIONS,
                # …and the notice ledger learns WHICH wall fired (runs after the table exists).
                *QUOTA_NOTICE_LIMIT_KIND_MIGRATIONS,
                # OPS-BOT-DISPATCH-LATENCY-W1 CH1 (2026-09-04): bounded per-bucket retry so a
                # failed MCP call stops silently consuming the bar.
                *FETCH_RETRY_MIGRATIONS,
            ):
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if (
                        "duplicate column name" not in msg
                        and "already exists" not in msg
                    ):
                        raise
            # SCAN-RANKBY-W1: widen scan_watches PK to include rank_by on EXISTING DBs.
            # Row-preserving + atomic + backed-up; idempotent (no-op once rank_by present).
            _migrate_scan_watches_rank_by(cur)
            # TG-SCANWATCH-TF-CADENCE-W1: add last_sent_sig AFTER the rank_by recreate so it
            # lands on the FINAL table shape regardless of migration order. Idempotent.
            for stmt in SCANWATCH_SIG_MIGRATIONS:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise

    def _enforce_mode_660(self) -> None:
        # Spec C2 line 180: state.db mode 660 owner algovault-bot:algovault-bot.
        # SQLite creates the file with the process's umask, so we re-chmod after init.
        # WAL/SHM siblings get the same treatment so concurrent C3 cron readers don't
        # see permission errors.
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            try:
                os.chmod(p, 0o660)
            except FileNotFoundError:
                pass

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = _connect(self.path)
        try:
            cur = conn.cursor()
            yield cur
        finally:
            conn.close()

    # ── subscribers ────────────────────────────────────────────

    def upsert_subscriber(
        self, chat_id: int, username: str | None, lang_code: str | None
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO subscribers(chat_id, username, lang_code, last_seen_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(chat_id) DO UPDATE SET
                  username     = excluded.username,
                  lang_code    = excluded.lang_code,
                  last_seen_at = excluded.last_seen_at
                """,
                (chat_id, username, lang_code),
            )

    def get_subscriber(self, chat_id: int) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM subscribers WHERE chat_id = ?", (chat_id,))
            return cur.fetchone()

    def count_subscribers(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM subscribers")
            return int(cur.fetchone()[0])

    # ── GROWTH-TG-CHANNEL-ACQUISITION-W1 (CH1): first-touch acquisition source ──

    def set_acquisition_source_first_touch(self, chat_id: int, source: str) -> bool:
        """Record the acquisition channel for this chat, FIRST TOUCH ONLY.

        Returns True iff this call is what set the value. The ``IS NULL`` guard in
        the WHERE clause is the immutability: a second /start carrying a different
        tag updates zero rows, so a later click can never claim a signup the first
        one earned. Idempotent — re-sending the SAME tag also returns False, which
        is correct (it did not set it; the first touch did).

        Caller must have upserted the subscriber row first; a missing row updates
        nothing and returns False rather than creating a sourced-but-unonboarded
        subscriber.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET acquisition_source = ? "
                "WHERE chat_id = ? AND acquisition_source IS NULL",
                (source, chat_id),
            )
            return cur.rowcount > 0

    def get_acquisition_source(self, chat_id: int) -> str | None:
        """First-touch acquisition channel, or None for a pre-CH1 / untagged join.

        None is meaningful and must stay None: CH2 emits the signup URL exactly as
        it did before this wave for these users (absence is absence, no empty param).
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT acquisition_source FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def count_by_acquisition_source(self) -> dict[str, int]:
        """Subscribers per channel, for the CH4 readout. NULL is reported under the
        key ``(untagged)`` rather than dropped — the untagged share IS the coverage
        ceiling on every channel number, so it must never silently vanish."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COALESCE(acquisition_source, '(untagged)') AS s, COUNT(*) "
                "FROM subscribers GROUP BY s ORDER BY COUNT(*) DESC"
            )
            return {str(r[0]): int(r[1]) for r in cur.fetchall()}

    def referral_mint_rate(self) -> tuple[int, int]:
        """(minted, total) subscribers — READ-ONLY, zero schema change.

        GROWTH-TG-LEVER-ACTIVATION-W1 (CH3). ``referral_code`` is written by
        ``set_referral_code`` when a user runs /referral, so a non-NULL value means
        that subscriber actually minted a shareable code.

        Retained as a STANDING OBSERVATION, not as anyone's primary metric. CH3's
        premise — that the referral loop was merely undiscovered — was falsified at
        Step 0: the value-moment nudge (``cta.referral_nudge_text``, live and
        ungated via ``alert_engine``) had already reached 19 of 50 subscribers and
        the mint rate was still 1 of 50. A referral loop is a multiplier on an
        engaged base; with 1-3 daily actives there is nothing to multiply. This
        exists so the successor activation wave has the control series — NOT to
        justify more surfacing.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE referral_code IS NOT NULL), COUNT(*) "
                "FROM subscribers"
            )
            row = cur.fetchone()
            return (int(row[0]), int(row[1]))

    # ── REFERRAL-PARITY-NOTIFS-W1 / C2: referral-code ↔ chat_id mapping ──
    def set_referral_code(self, chat_id: int, code: str) -> None:
        """Cache the engine's referral code for this chat (idempotent, set on /referral)."""
        with self._cursor() as cur:
            cur.execute("UPDATE subscribers SET referral_code = ? WHERE chat_id = ?", (code, chat_id))

    def chat_ids_for_referral_code(self, code: str) -> list[int]:
        """Non-blocked chat_ids whose cached referral_code matches (the drain's local map)."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT chat_id FROM subscribers WHERE referral_code = ? AND bot_blocked_at IS NULL",
                (code,),
            )
            return [int(r[0]) for r in cur.fetchall()]

    # ── TG-REFERRAL-W1: bot-side referee bonus-call pool ─────────────────

    def get_referral_bonus(self, chat_id: int) -> int:
        """Bot-side referee bonus calls remaining (0 if none / row absent)."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT referral_bonus_remaining FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def grant_referral_bonus(self, chat_id: int, calls: int) -> int:
        """Additively credit `calls` bonus calls to a subscriber; returns the new remaining.

        One-grant-per-referee is enforced UPSTREAM by the engine's attribution
        UNIQUE — the bot credits here only once the engine confirms a fresh
        attribution. Clamps negatives to 0 (default-deny). The subscriber row must
        already exist (the ref-join path upserts first); a missing row is a no-op.
        """
        add = max(0, int(calls))
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET referral_bonus_remaining = "
                "referral_bonus_remaining + ? WHERE chat_id = ?",
                (add, chat_id),
            )
            cur.execute(
                "SELECT referral_bonus_remaining FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def mark_referral_nudge_sent(self, chat_id: int, now_iso: str) -> None:
        """Stamp the value-moment referral nudge (the 7d throttle lives in
        cta.referral_nudge_text; this records when it last fired)."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET referral_nudge_last_at = ? WHERE chat_id = ?",
                (now_iso, chat_id),
            )

    # ── TG-WATCH-ADOPTION-BROADCAST-W1: first-watch onboarding nudge ──────

    def has_any_engagement(self, chat_id: int) -> bool:
        """True iff this subscriber has ≥1 watchlist OR ≥1 scan_watch row.

        The first-watch onboarding nudge targets subscribers with ZERO of
        either (passive subscribers who generate ~0 calls).
        """
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM watchlists WHERE chat_id = ? LIMIT 1", (chat_id,))
            if cur.fetchone() is not None:
                return True
            cur.execute("SELECT 1 FROM scan_watches WHERE chat_id = ? LIMIT 1", (chat_id,))
            return cur.fetchone() is not None

    def get_first_watch_nudge_sent_at(self, chat_id: int) -> str | None:
        """Return the ISO timestamp the first-watch nudge fired for this
        subscriber, or None if it never has (or the subscriber is absent)."""
        row = self.get_subscriber(chat_id)
        if row is None:
            return None
        return row["first_watch_nudge_sent_at"]

    def mark_first_watch_nudge_sent(self, chat_id: int, now_iso: str) -> None:
        """Set the one-time first-watch-nudge dedup flag. Idempotent in spirit —
        callers MUST check ``get_first_watch_nudge_sent_at(chat_id) is None``
        before sending so the nudge fires at most once per subscriber, ever."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET first_watch_nudge_sent_at = ? WHERE chat_id = ?",
                (now_iso, chat_id),
            )

    def list_zero_engagement_unnudged(self) -> list[int]:
        """Return chat_ids of REACHABLE subscribers (not bot-blocked) who have
        ZERO watchlist + ZERO scan_watch rows AND have never been sent the
        first-watch onboarding nudge. This is the batch-sweep target segment.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT s.chat_id FROM subscribers s
                WHERE s.bot_blocked_at IS NULL
                  AND s.first_watch_nudge_sent_at IS NULL
                  AND s.chat_id NOT IN (SELECT chat_id FROM watchlists)
                  AND s.chat_id NOT IN (SELECT chat_id FROM scan_watches)
                ORDER BY s.created_at ASC
                """
            )
            return [int(r[0]) for r in cur.fetchall()]

    # ── watchlists ─────────────────────────────────────────────

    def add_watch(
        self, chat_id: int, coin: str, timeframe: str, exchange: str, alert_type: str
    ) -> bool:
        """Insert a watchlist row. Returns True on insert, False if already present."""
        with self._cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO watchlists(chat_id, coin, timeframe, exchange, alert_type)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, coin, timeframe, exchange, alert_type),
                )
                return True
            except sqlite3.IntegrityError as e:
                if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                    # Update alert_type if the entry exists with a different type
                    cur.execute(
                        """
                        UPDATE watchlists SET alert_type = ?
                        WHERE chat_id = ? AND coin = ? AND timeframe = ? AND exchange = ?
                        """,
                        (alert_type, chat_id, coin, timeframe, exchange),
                    )
                    return False
                raise

    def remove_watch(
        self, chat_id: int, coin: str, timeframe: str, exchange: str
    ) -> bool:
        with self._cursor() as cur:
            cur.execute(
                """
                DELETE FROM watchlists
                WHERE chat_id = ? AND coin = ? AND timeframe = ? AND exchange = ?
                """,
                (chat_id, coin, timeframe, exchange),
            )
            return cur.rowcount > 0

    def add_watch_batch(
        self,
        chat_id: int,
        combos: "list[tuple[str, str, str]]",
        alert_type: str,
    ) -> int:
        """TG-BATCH-WATCHLIST-W1: insert many (coin, tf, exchange) rows in one
        ``executemany``. Idempotent — PK conflicts update ``alert_type`` instead
        of duplicating (SQLite 3.45+ ``ON CONFLICT DO UPDATE``). Returns the
        number of NEWLY-inserted rows (count delta; conflicts count as 0)."""
        if not combos:
            return 0
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM watchlists WHERE chat_id = ?", (chat_id,))
            before = int(cur.fetchone()[0])
            cur.executemany(
                """
                INSERT INTO watchlists(chat_id, coin, timeframe, exchange, alert_type)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, coin, timeframe, exchange)
                DO UPDATE SET alert_type = excluded.alert_type
                """,
                [(chat_id, c, t, x, alert_type) for (c, t, x) in combos],
            )
            cur.execute("SELECT COUNT(*) FROM watchlists WHERE chat_id = ?", (chat_id,))
            after = int(cur.fetchone()[0])
        return after - before

    def remove_watch_batch(
        self,
        chat_id: int,
        coin: str | None = None,
        timeframe: str | None = None,
        exchange: str | None = None,
    ) -> int:
        """TG-BATCH-WATCHLIST-W1: scoped bulk DELETE for one user. A ``None``
        dimension is a wildcard (no filter on it); a value filters exactly.
        Returns the number of rows removed."""
        clauses = ["chat_id = ?"]
        params: list[object] = [chat_id]
        if coin is not None:
            clauses.append("coin = ?")
            params.append(coin)
        if timeframe is not None:
            clauses.append("timeframe = ?")
            params.append(timeframe)
        if exchange is not None:
            clauses.append("exchange = ?")
            params.append(exchange)
        sql = f"DELETE FROM watchlists WHERE {' AND '.join(clauses)}"
        with self._cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def remove_all_watches(self, chat_id: int) -> int:
        """Remove every watchlist row for one user. Returns rows removed."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM watchlists WHERE chat_id = ?", (chat_id,))
            return cur.rowcount

    def list_watches(self, chat_id: int) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT coin, timeframe, exchange, alert_type, added_at
                FROM watchlists
                WHERE chat_id = ?
                ORDER BY added_at ASC
                """,
                (chat_id,),
            )
            return list(cur.fetchall())

    def count_watches(self, chat_id: int) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM watchlists WHERE chat_id = ?", (chat_id,))
            return int(cur.fetchone()[0])

    # ── FEATURE-PARITY-CHANNELS-W1 CH4 — scan_watches (scheduled scan-digest subs) ──

    def add_scan_watch(
        self, chat_id: int, top_n: int, timeframe: str, exchange: str, cadence: str,
        rank_by: str = "oi",
    ) -> bool:
        """Insert a scan-digest subscription. Returns True on insert, False if it already
        existed (updates the cadence in that case). SCAN-RANKBY-W1: `rank_by` is part of
        the watch identity (PK) — a chat can hold several lenses for one (top_n,tf,exchange);
        omitted ⇒ 'oi' (back-compat for the wizard / showcase / typed-no-lens callers)."""
        with self._cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO scan_watches(chat_id, top_n, timeframe, exchange, cadence, rank_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, top_n, timeframe, exchange, cadence, rank_by),
                )
                return True
            except sqlite3.IntegrityError as e:
                if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                    cur.execute(
                        "UPDATE scan_watches SET cadence = ? "
                        "WHERE chat_id = ? AND top_n = ? AND timeframe = ? AND exchange = ? AND rank_by = ?",
                        (cadence, chat_id, top_n, timeframe, exchange, rank_by),
                    )
                    return False
                raise

    def remove_scan_watch(
        self, chat_id: int, top_n: int, timeframe: str, exchange: str, rank_by: str = "oi"
    ) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM scan_watches "
                "WHERE chat_id = ? AND top_n = ? AND timeframe = ? AND exchange = ? AND rank_by = ?",
                (chat_id, top_n, timeframe, exchange, rank_by),
            )
            return cur.rowcount > 0

    def list_scan_watches(self, chat_id: int) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT chat_id, top_n, timeframe, exchange, cadence, last_fired_bucket, added_at, rank_by "
                "FROM scan_watches WHERE chat_id = ? ORDER BY added_at ASC",
                (chat_id,),
            )
            return list(cur.fetchall())

    def list_all_scan_watches(self) -> list[sqlite3.Row]:
        """Every scan-digest subscription (the cron's candidate set; due is computed in Python
        via timeframe_bucket_epoch — the re-scan bucket period is the row's OWN timeframe,
        TG-SCANWATCH-TF-CADENCE-W1). ``last_sent_sig`` drives content-dedup."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT chat_id, top_n, timeframe, exchange, cadence, last_fired_bucket, rank_by, "
                "last_sent_sig "
                "FROM scan_watches ORDER BY chat_id ASC"
            )
            return list(cur.fetchall())

    def mark_scan_watch_fired(
        self, chat_id: int, top_n: int, timeframe: str, exchange: str, bucket: int,
        rank_by: str = "oi", sig: str | None = None,
    ) -> None:
        # TG-SCANWATCH-TF-CADENCE-W1: `sig` (when not None) ALSO updates the content-dedup
        # signature (the last-delivered actionable set). Pass sig="" on an all-HOLD round so a
        # returning set re-fires; the new set's sig on a successful push; leave None (advance
        # the bucket only) for exhausted / deduped-unchanged owners.
        with self._cursor() as cur:
            if sig is None:
                cur.execute(
                    "UPDATE scan_watches SET last_fired_bucket = ? "
                    "WHERE chat_id = ? AND top_n = ? AND timeframe = ? AND exchange = ? AND rank_by = ?",
                    (bucket, chat_id, top_n, timeframe, exchange, rank_by),
                )
            else:
                cur.execute(
                    "UPDATE scan_watches SET last_fired_bucket = ?, last_sent_sig = ? "
                    "WHERE chat_id = ? AND top_n = ? AND timeframe = ? AND exchange = ? AND rank_by = ?",
                    (bucket, sig, chat_id, top_n, timeframe, exchange, rank_by),
                )

    def list_due_watches(self, now_epoch_seconds: int, tf_seconds: dict[str, int]) -> list[sqlite3.Row]:
        """Return rows due for the next cron fire (C3 consumer).

        SIGNAL-CLOSEDBAR-SHADOW-W1 CH6 — due-ness is now BUCKET-DETERMINISTIC:

            due iff target_epoch(tf, now) > target_epoch(tf, last_fetched_at)

        It used to be RELATIVE AGE (``now - last_fetched_at >= TF_SECONDS[tf]``) with the
        anchor re-stamped at fetch COMPLETION, seconds past the ``OnCalendar=*:*:00`` tick —
        so every fire slipped later than the last, forever. Measured on the live box:
        ``00:44:04 -> 01:45:07 -> ... -> 13:56:03``, exact +61min steps on a 1h row.

        A bucket is a function of the instant alone, so a fetch completing at :04 and one at
        :07 map to the SAME bucket and produce the same next due-time — the ratchet cannot
        accumulate. No new column, no migration: each row self-aligns on its first cycle and
        ``last_fetched_at`` keeps its existing meaning and its existing writer.

        ``tf_seconds`` stays in the signature (callers pass ``TF_SECONDS``) and still decides
        which timeframes are dispatchable at all, so an unknown timeframe is skipped exactly
        as before.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT chat_id, coin, timeframe, exchange, alert_type,
                       regime_last_seen, regime_pending, last_fetched_at,
                       last_verdict, last_verdict_streak, fetch_fail_streak
                FROM watchlists
                """
            )
            all_rows = list(cur.fetchall())
        # Filter in Python — keeps the SQL portable and the math local to TF_SECONDS.
        due: list[sqlite3.Row] = []
        for r in all_rows:
            if not tf_seconds.get(r["timeframe"], 0):
                continue
            # An unparseable stamp resolves to None ⇒ treated as never-fetched ⇒ due, which
            # preserves the prior behaviour: a row must never strand itself on a bad value.
            if is_due(
                r["timeframe"],
                now_epoch_seconds,
                _iso_to_epoch(r["last_fetched_at"]),
                r["chat_id"],
                r["coin"],
                r["exchange"],
            ):
                due.append(r)
        return due

    # ── BOT-W2: per-user signup attribution (linked_api_key / linked_tier) ──

    def link_subscriber(
        self, chat_id: int, api_key: str, tier: str
    ) -> tuple[str | None, bool]:
        """Bind chat_id to (api_key, tier).

        Returns ``(previous_tier, is_new_link)``:
        - ``previous_tier`` = the tier this chat was linked to before, or None
          if this is the first link (or the previous record was 'free'/None).
        - ``is_new_link`` = True iff this chat had no prior `linked_api_key`.
          Used by the handler to decide whether to send a "Linked!" DM (new)
          vs a "Tier updated" DM (re-link from upgrade) — C3 will use this.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT linked_api_key, linked_tier FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            row = cur.fetchone()
            previous_tier = row["linked_tier"] if row else None
            is_new_link = not (row and row["linked_api_key"])
            cur.execute(
                """
                UPDATE subscribers
                SET linked_api_key = ?,
                    linked_tier    = ?,
                    linked_at      = datetime('now')
                WHERE chat_id = ?
                """,
                (api_key, tier, chat_id),
            )
        return previous_tier, is_new_link

    def unlink_subscriber(self, chat_id: int) -> None:
        """Return this chat to the free tier.

        OPS-BOT-LINKED-TIER-REFRESH-W1 CH3 gave this its first production caller. The
        lifecycle counters are cleared in the SAME statement: a streak that survived an
        unlink would carry into whatever link came next and could downgrade a brand-new
        subscription on someone else's history. The plan mirror is left alone — it is
        already unreadable without a `linked_api_key`, and clearing it here would be a
        second place that decides what "unlinked" means.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET linked_api_key = NULL, linked_tier = NULL, "
                "linked_at = NULL, link_invalid_streak = 0, link_invalid_since = NULL "
                "WHERE chat_id = ?",
                (chat_id,),
            )

    # OPS-BOT-LINKED-TIER-REFRESH-W1 CH3 — `get_linked_state` was DELETED here.
    #
    # P5 measured it callerless in `src/` (tests only) after two prior waves, and 3c is
    # explicit: wire it or delete it, never leave it as-is. Nothing in this chapter needed
    # it — the revalidation loop already holds the row from `paid_linked_chat_ids()` — and
    # dark code that LOOKS wired is the L2b hazard this repo names: a stale entry rots into
    # a permission slip. Callers wanting the pair read `get_subscriber(chat_id)`, which
    # returns the whole row and costs the same one query.
    #
    # `unlink_subscriber` above got its first real caller in the same chapter: the
    # downgrade transition in `entitlement_drain._apply_link_observation`.

    # ── CH3: the link lifecycle counters ────────────────────────────────

    def advance_link_invalid_streak(self, chat_id: int) -> tuple[int, str | None]:
        """Record ONE determined-invalid observation. Returns ``(streak, since)``.

        `link_invalid_since` is stamped only when the streak starts, so the grace window
        measures WALL-CLOCK from the first determined negative rather than counting drain
        passes — see the note on LINK_LIFECYCLE_MIGRATIONS for why that distinction matters.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET "
                "link_invalid_streak = COALESCE(link_invalid_streak, 0) + 1, "
                "link_invalid_since = COALESCE(link_invalid_since, datetime('now')) "
                "WHERE chat_id = ?",
                (chat_id,),
            )
            cur.execute(
                "SELECT link_invalid_streak, link_invalid_since FROM subscribers "
                "WHERE chat_id = ?",
                (chat_id,),
            )
            row = cur.fetchone()
        if row is None:
            return 0, None
        return int(row["link_invalid_streak"] or 0), row["link_invalid_since"]

    def reset_link_invalid_streak(self, chat_id: int) -> None:
        """Clear the streak. Called on ANY `VALID` or `INDETERMINATE` observation —
        the counter measures sustained determined invalidity, so anything else ends it."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET link_invalid_streak = 0, link_invalid_since = NULL "
                "WHERE chat_id = ? AND (link_invalid_streak != 0 OR link_invalid_since IS NOT NULL)",
                (chat_id,),
            )

    def mark_link_downgrade_notified(self, chat_id: int) -> None:
        """Stamp the downgrade-notice episode, like `quota_100_last_fired_at`."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET link_downgrade_notice_at = datetime('now') "
                "WHERE chat_id = ?",
                (chat_id,),
            )

    # ── C4: per-subscriber counters for CTA logic + admin stats ─────

    def increment_total_regime_alerts(self, chat_id: int) -> int:
        """Increment + return the subscriber's lifetime regime-alert count."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET total_regime_alerts = total_regime_alerts + 1 "
                "WHERE chat_id = ? RETURNING total_regime_alerts",
                (chat_id,),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def increment_total_call_alerts(self, chat_id: int) -> int:
        """Increment + return the subscriber's lifetime trade-call-alert count."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET total_call_alerts = total_call_alerts + 1 "
                "WHERE chat_id = ? RETURNING total_call_alerts",
                (chat_id,),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def increment_total_ctas_shown(self, chat_id: int) -> int:
        """Increment + return the subscriber's lifetime CTA-shown count (admin /stats)."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET total_ctas_shown = total_ctas_shown + 1 "
                "WHERE chat_id = ? RETURNING total_ctas_shown",
                (chat_id,),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # ── ACTIVATION-FUNNEL-AUDIT-W1: first-command dedup flag ─────────────

    def get_first_command_fired_at(self, chat_id: int) -> str | None:
        """Return the raw `first_command_fired_at` ISO string for a subscriber,
        or None if the subscriber has never fired a non-/start command (or
        the row is absent).

        Used by handlers.py wrappers to dedup the `tg_bot_first_command`
        funnel-stage 11 emit per CLAUDE.md Q-C Option α — only fire the
        log_alert_event once per subscriber, ever.
        """
        row = self.get_subscriber(chat_id)
        if row is None:
            return None
        return row["first_command_fired_at"]

    def set_first_command_fired_at(self, chat_id: int, now_iso: str) -> None:
        """Set the `first_command_fired_at` flag for a subscriber (once-ever
        marker). Idempotent in spirit — callers should check
        `get_first_command_fired_at(chat_id) is None` BEFORE invoking, but
        re-calling this method just overwrites the same row.

        Used by handlers.py wrappers in coordination with the
        `tg_bot_first_command` funnel-stage 11 emit (ACTIVATION-FUNNEL-AUDIT-W1).
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET first_command_fired_at = ? WHERE chat_id = ?",
                (now_iso, chat_id),
            )

    # ── TG-BROADCAST-STACK-W1 C4: /unlock_premium_alerts state machine ────

    def get_unlock_state(self, chat_id: int) -> tuple[str | None, str | None, str | None, str | None]:
        """Return (unlock_status, unlock_method, unlock_screenshot_path,
        npm_unlock_session_id) for a subscriber, or (None, None, None, None)
        if not present.
        """
        row = self.get_subscriber(chat_id)
        if row is None:
            return None, None, None, None
        return (
            row["unlock_status"],
            row["unlock_method"],
            row["unlock_screenshot_path"],
            row["npm_unlock_session_id"],
        )

    def set_unlock_pending(
        self,
        chat_id: int,
        new_status: str,
        method: str,
        track_token: str | None = None,
    ) -> None:
        """Transition subscriber to pending_x_screenshot OR pending_npm_call.

        ``track_token`` is set only for the npm path; ignored for X path.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET unlock_status = ?, unlock_method = ?, "
                "npm_unlock_session_id = COALESCE(?, npm_unlock_session_id) "
                "WHERE chat_id = ?",
                (new_status, method, track_token, chat_id),
            )

    def set_unlock_screenshot_path(self, chat_id: int, path: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET unlock_screenshot_path = ? WHERE chat_id = ?",
                (path, chat_id),
            )

    def set_unlock_verified(self, chat_id: int, now_iso: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET unlock_status = 'verified', "
                "unlock_verified_at = ? WHERE chat_id = ?",
                (now_iso, chat_id),
            )

    def set_unlock_expired(self, chat_id: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET unlock_status = 'expired' WHERE chat_id = ?",
                (chat_id,),
            )

    def reset_unlock_state(self, chat_id: int) -> None:
        """Used by [Reject] callback — return subscriber to not_started so
        they can retry /unlock_premium_alerts with a clearer screenshot.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET unlock_status = NULL, "
                "unlock_method = NULL, unlock_screenshot_path = NULL "
                "WHERE chat_id = ?",
                (chat_id,),
            )

    def set_npm_unlock_detected_at(self, chat_id: int, now_iso: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET npm_unlock_detected_at = ? WHERE chat_id = ?",
                (now_iso, chat_id),
            )

    # ── TG-BROADCAST-STACK-W1 C4: tg_pro_grants CRUD ──────────────────────

    def get_pro_grant(self, chat_id: int) -> sqlite3.Row | None:
        """Return the active tg_pro_grants row for a subscriber, or None if
        no active grant. Caller checks ``expires_at > NOW()`` for liveness.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT chat_id, granted_at, expires_at, method "
                "FROM tg_pro_grants WHERE chat_id = ?",
                (chat_id,),
            )
            return cur.fetchone()

    def insert_or_replace_pro_grant(
        self, chat_id: int, expires_at_iso: str, method: str
    ) -> None:
        """Upsert a 30-day Pro grant. Per spec: ``chat_id PK`` so one active
        grant per subscriber at a time; re-grant (e.g. via second method)
        REPLACES the current grant.
        """
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO tg_pro_grants "
                "(chat_id, granted_at, expires_at, method) "
                "VALUES (?, datetime('now'), ?, ?)",
                (chat_id, expires_at_iso, method),
            )

    def find_subscriber_by_npm_token(self, track_token: str) -> sqlite3.Row | None:
        """Look up subscriber whose npm_unlock_session_id matches the given
        track_token AND is currently in state pending_npm_call. Used by
        scripts/check-npm-unlocks.py (C6) to map detected funnel_events
        rows back to a subscriber for grant issuance.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT chat_id, lang_code, npm_unlock_session_id "
                "FROM subscribers WHERE npm_unlock_session_id = ? "
                "AND unlock_status = 'pending_npm_call'",
                (track_token,),
            )
            return cur.fetchone()

    # ── BOT-ALERT-CLEANUP-W1: soft/urgent CTA per-threshold throttle ────

    def get_quota_cta_fired_at(self, chat_id: int) -> tuple[str | None, str | None]:
        """Returns ``(quota_75_last_fired_at, quota_90_last_fired_at)`` raw strings.

        Either may be ``None`` if the threshold has never fired for the user.
        Caller (``cta.py``) parses to ``datetime`` via ``quota._parse_ts``.
        """
        row = self.get_subscriber(chat_id)
        if row is None:
            return None, None
        return row["quota_75_last_fired_at"], row["quota_90_last_fired_at"]

    def mark_quota_cta_fired(self, chat_id: int, threshold: str, now_iso: str) -> None:
        """Record that the {threshold}% trade-call CTA was just shown to the user.

        ``threshold`` ∈ {'75', '90', '100'}. Writes ``quota_<threshold>_last_fired_at``.

        BOT-QUOTA-REFUSAL-SEAM-W1 (2026-08-16): '100' used to fall through to a
        SILENT no-op ("not throttled"), so any caller stamping the wall wrote
        nothing and got no error. The wall IS throttled now — once per exhaustion
        window, keyed on ``alerts_window_start`` (see ``quota._notice_due``) —
        so '100' is a real column. An unknown threshold still no-ops, but loudly.
        """
        col = {
            "75": "quota_75_last_fired_at",
            "90": "quota_90_last_fired_at",
            "100": "quota_100_last_fired_at",
        }.get(threshold)
        if col is None:
            log.warning("mark_quota_cta_fired: unknown threshold %r — no-op", threshold)
            return
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE subscribers SET {col} = ? WHERE chat_id = ?",
                (now_iso, chat_id),
            )

    def get_active_chat_ids(self) -> list[int]:
        """Every REACHABLE subscriber's chat_id (excludes bot-blocked).

        BOT-QUOTA-REFUSAL-SEAM-W1: the iteration set for ``quota.count_walled_now``.
        Deliberately returns ids only — the caller projects each one through
        ``evaluate_delivery`` so the walled count derives from the SAME decision
        the seam enforces, never from a second ``alert_count >= 100`` in SQL.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT chat_id FROM subscribers WHERE bot_blocked_at IS NULL"
            )
            return [int(r[0]) for r in cur.fetchall()]

    # ── PRICING-BOT-DELIVERY-METERING-W1 CH4: outbox + plan mirror ───

    def enqueue_entitlement_debit(
        self, idem_key: str, chat_id: int, kind: str, units: int = 1, channel: str = "bot"
    ) -> bool:
        """Queue ONE plan debit. Returns True if this call enqueued it.

        `INSERT OR IGNORE` on the UNIQUE `idem_key`: a duplicate is a no-op, not an error, because
        the same delivery must never be queued twice. Swallows nothing else — a real failure
        raises to the recorder, which is inside the delivery path's existing try/except.
        """
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO entitlement_outbox(idem_key, chat_id, channel, kind, units) "
                "VALUES (?, ?, ?, ?, ?)",
                (idem_key, chat_id, channel, kind, units),
            )
            return cur.rowcount > 0

    def pending_entitlement_debits(self, limit: int = 100) -> list[sqlite3.Row]:
        """Oldest-first pending batch, joined to the identity the drainer needs.

        `linked_api_key` is read HERE, at send time, never from the queue row — so an unlinked
        subscriber's queued rows resolve to NULL and terminate rather than charging a revoked key.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT o.*, s.linked_api_key, s.linked_tier FROM entitlement_outbox o "
                "LEFT JOIN subscribers s ON s.chat_id = o.chat_id "
                "WHERE o.sent_at IS NULL ORDER BY o.id LIMIT ?",
                (limit,),
            )
            return list(cur.fetchall())

    def mark_entitlement_debit_sent(self, row_id: int, last_error: str | None = None) -> None:
        """Terminal: stamp `sent_at`. `last_error` carries a terminal REASON (e.g. 'unlinked',
        'REFUSED') — a stamped row with a reason is a decision, not a silent drop."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE entitlement_outbox SET sent_at = datetime('now'), last_error = ? WHERE id = ?",
                (last_error, row_id),
            )

    def bump_entitlement_debit_attempt(self, row_id: int, last_error: str) -> None:
        """Non-terminal: the row stays pending and is retried with backoff."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE entitlement_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (last_error, row_id),
            )

    def count_pending_entitlement_debits(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM entitlement_outbox WHERE sent_at IS NULL")
            return int(cur.fetchone()[0])

    def count_unmetered_deliveries_last_24h(self) -> int:
        """Debits stamped TERMINAL because the key would not validate — the revenue-leak meter.

        OPS-VALIDATE-KEY-INDETERMINATE-W1 CH6. Every row counted here is an alert we DELIVERED
        and will never charge for: `entitlement_drain` stamps `key_invalid_404` and the row is
        never charged and never retried. Measured 2026-09-04 at 1,987 such rows for a single
        `past_due` customer across nine days, while `plan_units_debited` sat healthy and nothing
        anywhere said a word — the digest reported the debits that WORKED and had no denominator.

        A non-zero value is not automatically a fault: a genuinely cancelled subscriber's queued
        debits land here too, and that is correct. It is the SUSTAINED non-zero that means a live
        subscriber is being served for nothing.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM entitlement_outbox "
                "WHERE sent_at IS NOT NULL AND last_error LIKE 'key_invalid%' "
                "AND sent_at >= datetime('now', '-24 hours')"
            )
            return int(cur.fetchone()[0])

    def count_linked_by_entitlement_state(self) -> dict[str, int]:
        """Linked subscribers grouped by the state their mirror last observed.

        NULL groups under `unobserved` and is NEVER folded into any state: a mirror written by a
        server predating CH2 carries no state, and reporting that as ENTITLED would manufacture
        the very reassurance this line exists to withhold.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT COALESCE(plan_entitlement_state, 'unobserved') AS st, COUNT(*) AS n "
                "FROM subscribers WHERE linked_api_key IS NOT NULL GROUP BY st"
            )
            return {str(r["st"]): int(r["n"]) for r in cur.fetchall()}

    def count_plan_units_debited_last_24h(self) -> int:
        """Units the drainer CONFIRMED charged in the last 24h — `sent_at` set with no terminal
        error, i.e. CHARGED or ALREADY_CHARGED. A REFUSED or unlinked row carries a reason and is
        excluded: it is a decision, not a debit."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(units), 0) FROM entitlement_outbox "
                "WHERE sent_at >= datetime('now', '-1 day') AND last_error IS NULL"
            )
            return int(cur.fetchone()[0])

    def update_plan_mirror(self, chat_id: int, state: dict, source: str) -> None:
        """Write the server's answer into the local mirror.

        `total`/`remaining` arrive as JSON `null` for an uncapped tier — stored as NULL, which CH5
        reads as "no ceiling", NEVER as zero. `plan_state_as_of` is stamped here and ONLY here: it
        is the freshness key the wall's three-state decision turns on.
        """
        import json as _json
        nxt = state.get("next_plan")
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET plan_used = ?, plan_total = ?, plan_allowed = ?, "
                "plan_limit_kind = ?, plan_period_start = ?, plan_daily_day = ?, "
                "plan_next_json = ?, plan_tier = ?, plan_entitlement_state = ?, "
                "plan_state_as_of = datetime('now'), plan_state_source = ? "
                "WHERE chat_id = ?",
                (
                    state.get("used"),
                    state.get("total"),
                    1 if state.get("allowed") else 0,
                    state.get("limit"),
                    state.get("period_start"),
                    state.get("daily_day"),
                    _json.dumps(nxt) if nxt is not None else None,
                    # OPS-BOT-LINKED-TIER-REFRESH-W1 CH2: same 200 body, same call, same cadence.
                    # `tier` has been arriving in every consume/state response all along —
                    # verified live on both routes 2026-08-21 — and was simply discarded.
                    # A body without it stores NULL, which reads as UNOBSERVED and falls back;
                    # it never overwrites a known tier with nothing.
                    state.get("tier"),
                    # CH4/CH6 — same 200 body, same call, same cadence, as `tier` before it.
                    # A body without it stores NULL, which reads as UNOBSERVED; it never
                    # overwrites a known state with nothing.
                    state.get("entitlement_state"),
                    source,
                    chat_id,
                ),
            )

    def paid_linked_chat_ids(self) -> list[sqlite3.Row]:
        """Reachable subscribers with a linked key — the poll set that keeps idle mirrors warm."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT chat_id, linked_api_key, linked_tier, plan_tier, plan_state_as_of, "
                "plan_entitlement_state, "
                "lang_code, link_invalid_since, link_invalid_streak, link_downgrade_notice_at "
                "FROM subscribers "
                "WHERE linked_api_key IS NOT NULL AND bot_blocked_at IS NULL"
            )
            return list(cur.fetchall())

    def mark_plan_wall_notice_day(self, chat_id: int, day: str) -> None:
        """Stamp the DAILY wall's episode key. Separate from `quota_100_last_fired_at` because the
        daily cap re-arms every UTC day — reusing the monthly stamp would send at most one notice
        ever (CH5c)."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET plan_wall_notice_day = ? WHERE chat_id = ?", (day, chat_id)
            )

    # ── GROWTH-TG-QUOTA-PARITY-W1 CH2: the FREE lane's daily wall + the ladder mirror ──

    def mark_quota_day_notice(self, chat_id: int, day: str) -> None:
        """Stamp the FREE lane's DAILY-wall episode key.

        The free-lane sibling of `mark_plan_wall_notice_day`, and separate from it for the same
        reason that one is separate from `quota_100_last_fired_at`: three lanes wall on three
        clocks, so they need three stamps. Collapsing any pair silences a lane.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET quota_day_notice_day = ? WHERE chat_id = ?",
                (day, chat_id),
            )

    def get_free_tier_ladder(self) -> sqlite3.Row | None:
        """The mirrored ladder row, or None when it has never been fetched.

        None is a FACT, not an error: on a box that has not yet run a drain since the migration
        there is genuinely no mirror, and the caller serves the pinned fallbacks. It never refuses.
        """
        with self._cursor() as cur:
            try:
                cur.execute(
                    "SELECT free_monthly, free_daily, starter_price_usd, starter_monthly_calls, "
                    "starter_daily_calls, starter_price_usd_6month, pro_price_usd, "
                    "pro_monthly_calls, pro_daily_calls, pro_price_usd_6month, "
                    "fetched_at FROM free_tier_ladder WHERE id = 1"
                )
            except sqlite3.OperationalError:
                # A DB that predates EITHER migration. Same tolerance as the per-row mirror
                # columns — and `quota._row_get` gives the caller a second, per-column layer,
                # because a partial migration must degrade one figure rather than the ladder.
                return None
            return cur.fetchone()

    def upsert_free_tier_ladder(
        self,
        free_monthly: int,
        free_daily: int,
        starter_price_usd: float | None,
        starter_monthly_calls: int | None,
        fetched_at: str,
        *,
        starter_daily_calls: int | None = None,
        starter_price_usd_6month: float | None = None,
        pro_price_usd: float | None = None,
        pro_monthly_calls: int | None = None,
        pro_daily_calls: int | None = None,
        pro_price_usd_6month: float | None = None,
    ) -> None:
        """Replace the single mirror row. `id = 1` is pinned by the table's own CHECK.

        GROWTH-TG-PLAN-PICKER-W1 R2 added the six keyword-only rungs. They default to None so
        every pre-existing caller and fixture emits an IDENTICAL row — absence writes NULL, and
        `quota.resolve_ladder` reads NULL as "serve the pinned constant". Keyword-only because a
        ten-argument positional call is where a price and a call count get swapped.
        """
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO free_tier_ladder "
                "(id, free_monthly, free_daily, starter_price_usd, starter_monthly_calls, "
                " starter_daily_calls, starter_price_usd_6month, pro_price_usd, "
                " pro_monthly_calls, pro_daily_calls, pro_price_usd_6month, fetched_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  free_monthly = excluded.free_monthly,"
                "  free_daily = excluded.free_daily,"
                "  starter_price_usd = excluded.starter_price_usd,"
                "  starter_monthly_calls = excluded.starter_monthly_calls,"
                "  starter_daily_calls = excluded.starter_daily_calls,"
                "  starter_price_usd_6month = excluded.starter_price_usd_6month,"
                "  pro_price_usd = excluded.pro_price_usd,"
                "  pro_monthly_calls = excluded.pro_monthly_calls,"
                "  pro_daily_calls = excluded.pro_daily_calls,"
                "  pro_price_usd_6month = excluded.pro_price_usd_6month,"
                "  fetched_at = excluded.fetched_at",
                (
                    free_monthly,
                    free_daily,
                    starter_price_usd,
                    starter_monthly_calls,
                    starter_daily_calls,
                    starter_price_usd_6month,
                    pro_price_usd,
                    pro_monthly_calls,
                    pro_daily_calls,
                    pro_price_usd_6month,
                    fetched_at,
                ),
            )

    # ── BOT-ZOMBIE-W1: bot-blocked subscriber bookkeeping ────────────

    def mark_subscriber_blocked(self, chat_id: int, now_iso: str) -> None:
        """Record that bot.send_* returned Forbidden ("bot was blocked by the
        user") for this chat. Digest/stats exclude subscribers with a
        non-null ``bot_blocked_at`` so the count reflects reachable users.
        Idempotent — re-blocking just updates the timestamp."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET bot_blocked_at = ? WHERE chat_id = ?",
                (now_iso, chat_id),
            )

    def unmark_subscriber_blocked(self, chat_id: int) -> None:
        """Clear ``bot_blocked_at`` — called from handle_start when a previously
        blocked subscriber sends /start again (i.e. they've unblocked).
        No-op if the column was already NULL."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET bot_blocked_at = NULL "
                "WHERE chat_id = ? AND bot_blocked_at IS NOT NULL",
                (chat_id,),
            )

    def count_active_subscribers(self) -> int:
        """Count subscribers who haven't blocked the bot."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM subscribers WHERE bot_blocked_at IS NULL")
            return int(cur.fetchone()[0])

    def count_blocked_subscribers(self) -> int:
        """Count subscribers who HAVE blocked the bot (zombies)."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM subscribers WHERE bot_blocked_at IS NOT NULL")
            return int(cur.fetchone()[0])

    # ── BOT-DIGEST-LAST24H-W1: per-alert log for rolling-24h digest ──

    def record_alert_fired(self, chat_id: int, kind: str, source: str = "watch") -> int:
        """Record one successful Telegram alert delivery for the rolling-24h
        digest count. ``kind`` ∈ {'regime', 'call'}; ``source`` ∈
        ALLOWED_ALERT_SOURCES discriminates the delivery path (watch push /
        scanwatch digest / on-demand scan; webhook+batch reserved). Quota-
        exhausted notices are operator UX nudges, not signal volume, so they are
        NOT recorded. ``fired_at`` defaults to ``datetime('now')`` (UTC).

        RETURNS the new ``alerts_fired.id`` (PRICING-BOT-DELIVERY-METERING-W1 CH4b). That id IS
        the entitlement idempotency source: ``bot:<chat_id>:<id>``. It is an
        INTEGER PRIMARY KEY AUTOINCREMENT written on exactly the event being billed, so it is
        globally unique per bot and monotonic — no clock, no UUID, no counter. Existing callers
        that ignore the return value are unaffected.
        Prefer the ``quota.record_call_delivered`` / ``record_regime_delivered``
        recorders (BOT-DIGEST-COUNT-ALL-CALLS-W1) so every delivery both logs
        here AND meters quota from ONE seam — do not call this raw on a new path."""
        if kind not in ("regime", "call"):
            raise ValueError(f"alerts_fired.kind must be 'regime' or 'call', got {kind!r}")
        if source not in ALLOWED_ALERT_SOURCES:
            raise ValueError(
                f"alerts_fired.source must be one of {sorted(ALLOWED_ALERT_SOURCES)}, got {source!r}"
            )
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO alerts_fired(chat_id, kind, source) VALUES (?, ?, ?)",
                (chat_id, kind, source),
            )
            return int(cur.lastrowid or 0)

    def count_alerts_fired_last_24h(self) -> tuple[int, int]:
        """Return ``(regime_count, call_count)`` for the rolling-24h window
        ending now. UTC throughout; SQLite ``datetime('now')`` is UTC by
        default, matching the digest cron fire time (03:00 UTC nightly)."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT kind, COUNT(*) FROM alerts_fired "
                "WHERE fired_at >= datetime('now', '-1 day') "
                "GROUP BY kind"
            )
            counts = {row[0]: int(row[1]) for row in cur.fetchall()}
        return counts.get("regime", 0), counts.get("call", 0)

    # ── BOT-DIGEST-QUOTA-NOTICES-W1: per-notice log for rolling-24h digest ──

    def record_quota_notice_fired(self, chat_id: int, limit_kind: str | None = None) -> None:
        """Record one successfully-delivered quota-exhausted notice for the
        rolling-24h digest line. Called from ``alert_engine`` AFTER the
        Telegram API returns OK on the quota-exhausted trade-call branch —
        failed sends are not counted. Distinct from ``record_alert_fired``:
        these are operator-UX nudges (the watcher is at their free cap),
        NOT signal volume, so they live in their own table and never
        inflate the regime/call counts. ``fired_at`` defaults to
        ``datetime('now')`` (UTC) at the DB layer.

        ``limit_kind`` ∈ {'monthly', 'daily', None} — WHICH wall fired.

        🛑 THIS IS THE ONLY DURABLE RECORD THAT A DAILY WALL BOUND. `alerts_fired` cannot
        show it: refusals never reach that table, so it is censored at the cap by
        construction. `subscribers.quota_day_notice_day` holds only the LAST such day and is
        overwritten. Without this column `GROWTH-TG-DAILY-CAP-IMPACT-W1` would read a
        confident zero regardless of the truth. See QUOTA_NOTICE_LIMIT_KIND_MIGRATIONS.

        Defaulted so the paid lane's callers keep working unchanged; None means "not recorded"
        and is what every pre-2026-08-27 row carries."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO quota_notices_fired(chat_id, limit_kind) VALUES (?, ?)",
                (chat_id, limit_kind),
            )

    def count_quota_notices_last_24h(self) -> int:
        """Return the count of quota-exhausted notices delivered in the
        rolling-24h window ending now. UTC throughout (SQLite
        ``datetime('now')`` is UTC), matching the 03:00-UTC digest fire."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM quota_notices_fired "
                "WHERE fired_at >= datetime('now', '-1 day')"
            )
            return int(cur.fetchone()[0])

    def update_watch_after_fetch(
        self,
        chat_id: int,
        coin: str,
        timeframe: str,
        exchange: str,
        last_verdict: str,
        last_verdict_streak: int,
        regime_last_seen: str | None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE watchlists
                SET last_fetched_at = datetime('now'),
                    last_verdict = ?,
                    last_verdict_streak = ?,
                    regime_last_seen = COALESCE(?, regime_last_seen),
                    fetch_fail_streak = 0
                WHERE chat_id = ? AND coin = ? AND timeframe = ? AND exchange = ?
                """,
                (last_verdict, last_verdict_streak, regime_last_seen, chat_id, coin, timeframe, exchange),
            )

    def consume_quota_atomic(
        self,
        chat_id: int,
        units: int,
        monthly_total: int,
        day_key: str,
        now_iso: str,
    ) -> tuple[int, int, int, str | None] | None:
        """OPS-BOT-DISPATCH-LATENCY-W1 CH2 — the free meter's charge, as ONE statement.

        `consume_quota` was a read-modify-write: `get_quota_state` read `alert_count`, Python
        computed `used + units`, and an UPDATE wrote the ABSOLUTE result. Two charges
        interleaving on one subscriber therefore both read N and both wrote N+1 — one delivered
        alert billed to nobody.

        That is not theoretical here and it does not need concurrency inside the engine to
        happen: `record_call_delivered` is called from the cron engine AND from `handlers.py`
        inside the separate, always-running `algovault-bot.service`, and both open the SAME
        `/var/lib/algovault-bot/state.db`. A user pressing a button while their watch tick
        fires is the whole reproduction.

        Every counter is now RELATIVE and derived inside the statement. SQLite evaluates every
        SET expression against the row's PRE-UPDATE values, so `alert_count` on the right-hand
        side of the bonus arm is the same snapshot the monthly arm used — the two cannot
        disagree, which a two-statement version could not guarantee at any isolation level we
        control.

        Precedent for the idiom, on this very table, three columns away:
        `increment_total_call_alerts` / `increment_total_regime_alerts` /
        `increment_total_ctas_shown` (`UPDATE … SET col = col + 1 … RETURNING col`). The
        counters that fund the product were the ones still doing it the unsafe way.

        `referral_bonus_remaining` is covered deliberately, not incidentally: it carries granted
        user value, so fixing only `alert_count` would close the lost-update class on the
        counter nobody was losing and leave it open on the one that costs a user something.

        Returns `(alert_count, referral_bonus_remaining, alerts_day_count, alerts_window_start)`
        after the write, or None when no such subscriber exists.
        """
        # The monthly charge: capped at remaining headroom ONLY when a bonus pool exists, which
        # is what makes the overflow land on the bonus. With no pool the meter is uncapped and
        # the user crosses the wall — byte-identical to the pre-wave Python for that (today:
        # 100%) base, which is why this reads as two arms rather than one tidy expression.
        monthly_charge = (
            "CASE WHEN COALESCE(referral_bonus_remaining, 0) > 0 "
            "THEN MIN(?, MAX(0, ? - alert_count)) ELSE ? END"
        )
        sql = f"""
            UPDATE subscribers
            SET alert_count = alert_count + ({monthly_charge}),
                referral_bonus_remaining =
                    CASE WHEN COALESCE(referral_bonus_remaining, 0) > 0
                         THEN MAX(0, referral_bonus_remaining - (? - ({monthly_charge})))
                         ELSE 0 END,
                -- Rolled on WRITE as well as on read: a stale day contributes 0, so a
                -- subscriber walled yesterday is served today with nothing having run overnight.
                alerts_day_count =
                    CASE WHEN alerts_day = ? THEN COALESCE(alerts_day_count, 0) ELSE 0 END + ?,
                alerts_day = ?,
                -- Replaces the `if state.window_start is None` branch. COALESCE is the same
                -- decision expressed atomically, so two first-charges cannot each start a window.
                alerts_window_start = COALESCE(alerts_window_start, ?)
            WHERE chat_id = ?
            RETURNING alert_count, referral_bonus_remaining, alerts_day_count, alerts_window_start
        """
        params = (
            units, monthly_total, units,               # monthly arm
            units, units, monthly_total, units,        # bonus arm (units, then the same CASE)
            day_key, units, day_key,                   # daily meter
            now_iso, chat_id,
        )
        with self._cursor() as cur:
            row = cur.execute(sql, params).fetchone()
        if row is None:
            return None
        return (
            int(row["alert_count"] or 0),
            int(row["referral_bonus_remaining"] or 0),
            int(row["alerts_day_count"] or 0),
            row["alerts_window_start"],
        )

    def record_fetch_failure(
        self,
        chat_id: int,
        coin: str,
        timeframe: str,
        exchange: str,
        last_verdict: str,
        last_verdict_streak: int,
        regime_last_seen: str | None,
        max_attempts: int = MAX_FETCH_ATTEMPTS_PER_BUCKET,
    ) -> tuple[int, bool]:
        """The failure counterpart of :meth:`update_watch_after_fetch`.

        Persists what the tick LEARNED (verdict / streak / regime_last_seen — flap-suppression
        state, which must survive a non-delivery exactly as it survives a quota refusal) while
        holding the dispatch bucket OPEN so the row is retried on the next tick.

        Returns ``(attempts_used, bucket_advanced)``.

        ONE STATEMENT, deliberately. Read-then-write here would be a lost-update race against
        the interactive `algovault-bot.service`, which shares this database file — the same
        class `AOE-RETUNE-IDEMPOTENCY-W1` ruled on ("a last line of defence may not have a race
        in it"). Both CASE arms read the PRE-update `fetch_fail_streak`, so the increment, the
        give-up test and the reset are one atomic decision.

        On the give-up tick the counter returns to 0 rather than staying at the cap: the cap is
        per BUCKET, and the bucket is over the moment `last_fetched_at` advances.
        """
        with self._cursor() as cur:
            row = cur.execute(
                """
                UPDATE watchlists
                SET last_verdict = ?,
                    last_verdict_streak = ?,
                    regime_last_seen = COALESCE(?, regime_last_seen),
                    fetch_fail_streak =
                        CASE WHEN fetch_fail_streak + 1 >= ? THEN 0
                             ELSE fetch_fail_streak + 1 END,
                    last_fetched_at =
                        CASE WHEN fetch_fail_streak + 1 >= ? THEN datetime('now')
                             ELSE last_fetched_at END
                WHERE chat_id = ? AND coin = ? AND timeframe = ? AND exchange = ?
                RETURNING fetch_fail_streak, last_fetched_at
                """,
                (
                    last_verdict, last_verdict_streak, regime_last_seen,
                    max_attempts, max_attempts,
                    chat_id, coin, timeframe, exchange,
                ),
            ).fetchone()
        if row is None:
            # Row deleted mid-tick (an /unwatch between dispatch and failure). Nothing to
            # retry and nothing to hold open.
            return (0, True)
        # fetch_fail_streak reads 0 on the give-up tick, so derive `advanced` from the counter
        # rather than re-reading the clock: 0 after a failure means the CASE took the cap arm.
        advanced = row["fetch_fail_streak"] == 0
        return (max_attempts if advanced else row["fetch_fail_streak"], advanced)
