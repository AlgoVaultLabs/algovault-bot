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
from pathlib import Path
from typing import Final, Iterator


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
  alert_type           TEXT NOT NULL DEFAULT 'both'
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
            for stmt in C4_MIGRATIONS:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e):
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

    def list_due_watches(self, now_epoch_seconds: int, tf_seconds: dict[str, int]) -> list[sqlite3.Row]:
        """Return rows due for the next cron fire (C3 consumer).

        A row is due iff ``now - last_fetched_at >= TF_SECONDS[timeframe]`` OR
        ``last_fetched_at IS NULL`` (never fetched).
        """
        with self._cursor() as cur:
            params = [(tf, secs) for tf, secs in tf_seconds.items()]
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
            secs = tf_seconds.get(r["timeframe"], 0)
            if not secs:
                continue
            if r["last_fetched_at"] is None:
                due.append(r)
                continue
            # last_fetched_at stored as ISO datetime; compare via parse.
            from datetime import datetime, timezone

            try:
                ts = datetime.fromisoformat(r["last_fetched_at"]).replace(tzinfo=timezone.utc)
                age = now_epoch_seconds - int(ts.timestamp())
                if age >= secs:
                    due.append(r)
            except (ValueError, TypeError):
                due.append(r)
        return due

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
