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
                       last_verdict, last_verdict_streak
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
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET linked_api_key = NULL, linked_tier = NULL, "
                "linked_at = NULL WHERE chat_id = ?",
                (chat_id,),
            )

    def get_linked_state(self, chat_id: int) -> tuple[str | None, str | None]:
        """Return ``(linked_api_key, linked_tier)`` for the chat, or (None, None)."""
        row = self.get_subscriber(chat_id)
        if row is None:
            return None, None
        return row["linked_api_key"], row["linked_tier"]

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

        ``threshold`` ∈ {'75', '90'}. Writes ``quota_<threshold>_last_fired_at``.
        """
        if threshold == "75":
            col = "quota_75_last_fired_at"
        elif threshold == "90":
            col = "quota_90_last_fired_at"
        else:
            return  # '100' is not throttled — no-op
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE subscribers SET {col} = ? WHERE chat_id = ?",
                (now_iso, chat_id),
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

    def record_alert_fired(self, chat_id: int, kind: str, source: str = "watch") -> None:
        """Record one successful Telegram alert delivery for the rolling-24h
        digest count. ``kind`` ∈ {'regime', 'call'}; ``source`` ∈
        ALLOWED_ALERT_SOURCES discriminates the delivery path (watch push /
        scanwatch digest / on-demand scan; webhook+batch reserved). Quota-
        exhausted notices are operator UX nudges, not signal volume, so they are
        NOT recorded. ``fired_at`` defaults to ``datetime('now')`` (UTC).
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

    def record_quota_notice_fired(self, chat_id: int) -> None:
        """Record one successfully-delivered quota-exhausted notice for the
        rolling-24h digest line. Called from ``alert_engine`` AFTER the
        Telegram API returns OK on the quota-exhausted trade-call branch —
        failed sends are not counted. Distinct from ``record_alert_fired``:
        these are operator-UX nudges (the watcher is at their 100/mo free
        cap), NOT signal volume, so they live in their own table and never
        inflate the regime/call counts. ``fired_at`` defaults to
        ``datetime('now')`` (UTC) at the DB layer."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO quota_notices_fired(chat_id) VALUES (?)",
                (chat_id,),
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
                    regime_last_seen = COALESCE(?, regime_last_seen)
                WHERE chat_id = ? AND coin = ? AND timeframe = ? AND exchange = ?
                """,
                (last_verdict, last_verdict_streak, regime_last_seen, chat_id, coin, timeframe, exchange),
            )
