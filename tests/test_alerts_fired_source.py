"""BOT-DIGEST-COUNT-ALL-CALLS-W1 — the alerts_fired.source migration, the delivery
recorder seam (alerts_fired INSERT + consume_quota from ONE place), the digest
quota↔telemetry parity canary, and the per-source digest render."""
from __future__ import annotations

import pytest

from algovault_bot import digest
from algovault_bot.db import ALLOWED_ALERT_SOURCES, Database
from algovault_bot.quota import (
    get_quota_state,
    record_call_delivered,
    record_regime_delivered,
)


def _rows_by_source(db: Database, kind: str = "call") -> dict[str, int]:
    with db._cursor() as cur:
        cur.execute(
            "SELECT source, COUNT(*) FROM alerts_fired WHERE kind=? GROUP BY source", (kind,)
        )
        return {r[0]: int(r[1]) for r in cur.fetchall()}


def _alert_count(db: Database, chat_id: int) -> int:
    with db._cursor() as cur:
        cur.execute("SELECT alert_count FROM subscribers WHERE chat_id=?", (chat_id,))
        return int(cur.fetchone()[0])


def _make_paid(db: Database, chat_id: int) -> None:
    with db._cursor() as cur:
        cur.execute("UPDATE subscribers SET linked_tier='starter' WHERE chat_id=?", (chat_id,))


# ── migration ───────────────────────────────────────────────────────────────

def test_source_column_present_and_backfills_watch(tmp_db: Database) -> None:
    with tmp_db._cursor() as cur:
        cols = [r[1] for r in cur.execute("PRAGMA table_info(alerts_fired)").fetchall()]
    assert "source" in cols
    # a row inserted WITHOUT a source (the historical shape) defaults to 'watch' —
    # the DEFAULT backfill semantics that retro-tag every pre-wave row.
    with tmp_db._cursor() as cur:
        cur.execute("INSERT INTO alerts_fired(chat_id, kind) VALUES (1, 'call')")
    assert _rows_by_source(tmp_db).get("watch") == 1


def test_migration_idempotent_runs_twice(tmp_db: Database) -> None:
    # _init_schema already ran once (fixture). Run twice more — no error (the
    # "duplicate column name" guard), and the column stays singular.
    tmp_db._init_schema()
    tmp_db._init_schema()
    with tmp_db._cursor() as cur:
        cols = [r[1] for r in cur.execute("PRAGMA table_info(alerts_fired)").fetchall()]
    assert cols.count("source") == 1


def test_allowed_sources_enum() -> None:
    assert ALLOWED_ALERT_SOURCES == frozenset({"watch", "scanwatch", "scan", "webhook", "batch"})


def test_record_alert_fired_rejects_unknown_source(tmp_db: Database) -> None:
    with pytest.raises(ValueError):
        tmp_db.record_alert_fired(1, "call", "bogus")


# ── recorder: insert + meter (free) / insert-only (paid) ─────────────────────

def test_recorder_free_user_inserts_and_charges(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(7, "u", "en")
    record_call_delivered(tmp_db, 7, "scanwatch")
    record_regime_delivered(tmp_db, 7, "watch")
    assert _rows_by_source(tmp_db, "call") == {"scanwatch": 1}
    assert _rows_by_source(tmp_db, "regime") == {"watch": 1}
    assert get_quota_state(tmp_db, 7).used == 2  # both call + regime count


def test_recorder_paid_user_inserts_but_does_not_charge(tmp_db: Database) -> None:
    # the volume-vs-billing split: a paid tier STILL logs delivery volume (digest),
    # but consume_quota is a no-op (their billing runs server-side).
    tmp_db.upsert_subscriber(8, "u", "en")
    _make_paid(tmp_db, 8)
    record_call_delivered(tmp_db, 8, "scan")
    record_call_delivered(tmp_db, 8, "scan")
    assert _rows_by_source(tmp_db, "call") == {"scan": 2}
    assert get_quota_state(tmp_db, 8).used == 0
    assert _alert_count(tmp_db, 8) == 0


# ── parity canary (single-derivation lock: telemetry == quota) ───────────────

def test_parity_free_K_calls_K_rows_K_quota(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(9, "u", "en")
    K = 5  # a scanwatch/scan round delivering K non-HOLD calls (HOLD never reaches here)
    for _ in range(K):
        record_call_delivered(tmp_db, 9, "scanwatch")
    assert sum(_rows_by_source(tmp_db, "call").values()) == K
    assert get_quota_state(tmp_db, 9).used == K  # the lock: telemetry == quota


def test_parity_paid_K_rows_zero_quota(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(10, "u", "en")
    _make_paid(tmp_db, 10)
    K = 4
    for _ in range(K):
        record_call_delivered(tmp_db, 10, "scan")
    assert sum(_rows_by_source(tmp_db, "call").values()) == K
    assert get_quota_state(tmp_db, 10).used == 0


def test_watch_path_no_double_count(tmp_db: Database) -> None:
    # one delivered watch call ⇒ exactly one alerts_fired row + one quota unit
    # (the recorder REPLACED the old separate INSERT+consume pair — no double-count).
    tmp_db.upsert_subscriber(11, "u", "en")
    record_call_delivered(tmp_db, 11, "watch")
    assert sum(_rows_by_source(tmp_db, "call").values()) == 1
    assert get_quota_state(tmp_db, 11).used == 1


# ── digest render: 3 labeled sub-sources incl a zero; total == sum ───────────

def test_digest_renders_three_sources_with_total(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(12, "u", "en")
    for _ in range(2):
        record_call_delivered(tmp_db, 12, "watch")
    for _ in range(3):
        record_call_delivered(tmp_db, 12, "scanwatch")
    # source='scan' deliberately zero → must STILL show (the whole point: a silently
    # zeroed path is visible at a glance).
    body = digest.render_digest(tmp_db)
    assert "📈 Calls: 5" in body
    assert "👁 Watch 2" in body
    assert "🔭 Scanwatch 3" in body
    assert "🔎 Scan 0" in body
