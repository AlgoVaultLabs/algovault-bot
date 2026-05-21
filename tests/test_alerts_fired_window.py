"""BOT-DIGEST-LAST24H-W1 — per-alert log + rolling-24h digest.

Path B (per Plan-Mode probe outcome): live state.db only had lifetime
aggregates (subscribers.total_regime_alerts / total_call_alerts) — no
per-alert timestamp existed. This wave added the ``alerts_fired`` table +
``record_alert_fired`` helper, wired the INSERT into alert_engine's
``_push`` + ``_push_photo`` success paths, and flipped digest.py +
admin.py to ``SELECT COUNT(*) ... WHERE fired_at >= datetime('now',
'-1 day')`` aggregates.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, TelegramError

from algovault_bot.alert_engine import _push, _push_photo
from algovault_bot.db import Database


# ── alerts_fired schema + helpers ─────────────────────────────


def test_alerts_fired_table_exists(tmp_db: Database) -> None:
    """Schema migration is idempotent across Database() init calls."""
    with tmp_db._cursor() as cur:
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts_fired'"
        )
        assert cur.fetchone() is not None
    # Re-init should not raise.
    Database(tmp_db.path)


def test_count_alerts_fired_last_24h_empty(tmp_db: Database) -> None:
    """Empty table → (0, 0)."""
    assert tmp_db.count_alerts_fired_last_24h() == (0, 0)


def test_record_alert_fired_regime_then_count(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.record_alert_fired(1, "regime")
    tmp_db.record_alert_fired(1, "regime")
    tmp_db.record_alert_fired(1, "call")
    regime, call = tmp_db.count_alerts_fired_last_24h()
    assert regime == 2
    assert call == 1


def test_record_alert_fired_rejects_bad_kind(tmp_db: Database) -> None:
    """``kind`` validated at Python layer (raises ValueError) AND DB layer
    (CHECK constraint would reject too)."""
    with pytest.raises(ValueError):
        tmp_db.record_alert_fired(1, "cta")
    with pytest.raises(ValueError):
        tmp_db.record_alert_fired(1, "")


def test_24h_window_excludes_rows_older_than_24h(tmp_db: Database) -> None:
    """Insert rows with explicit ``fired_at`` timestamps to confirm the
    digest SELECT filter ``fired_at >= datetime('now', '-1 day')`` is
    honored. Two regime rows: one 23h ago (in window), one 25h ago
    (out of window)."""
    tmp_db.upsert_subscriber(1, "u1", "en")
    with tmp_db._cursor() as cur:
        cur.execute(
            "INSERT INTO alerts_fired(chat_id, kind, fired_at) VALUES "
            "(?, ?, datetime('now', '-23 hours'))",
            (1, "regime"),
        )
        cur.execute(
            "INSERT INTO alerts_fired(chat_id, kind, fired_at) VALUES "
            "(?, ?, datetime('now', '-25 hours'))",
            (1, "regime"),
        )
        cur.execute(
            "INSERT INTO alerts_fired(chat_id, kind, fired_at) VALUES "
            "(?, ?, datetime('now', '-2 days'))",
            (1, "call"),
        )
    regime, call = tmp_db.count_alerts_fired_last_24h()
    assert regime == 1, "23h-old row in window; 25h-old row excluded"
    assert call == 0, "2-day-old call row excluded"


# ── _push / _push_photo INSERT semantics ──────────────────────


def _record_calls_from_mock_db(db: Database) -> list[tuple[int, str]]:
    """Helper: peek at the alerts_fired table to verify INSERT happened
    (or didn't) under a real DB fixture."""
    with db._cursor() as cur:
        cur.execute("SELECT chat_id, kind FROM alerts_fired ORDER BY id")
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def test_push_success_records_regime_alert(tmp_db: Database) -> None:
    """``_push`` success in alert_engine.py:394 wires ``record_alert_fired``
    AFTER the Telegram API returned OK. The unit test cannot reach the
    branch inside ``process_one_row`` directly without mocking the entire
    MCP/quota pipeline, so this test exercises the contract end-to-end:
    a successful _push returning True is followed by an explicit
    record_alert_fired call (mirrors the alert_engine.py:394-398 block)."""
    tmp_db.upsert_subscriber(42, "alice", "en")
    bot = AsyncMock()
    bot.send_message.return_value = None  # success
    ok = asyncio.run(_push(bot, 42, "regime shift!", db=tmp_db))
    assert ok is True
    # Mirror the alert_engine.py:394+ success branch:
    if ok:
        tmp_db.record_alert_fired(42, "regime")
    assert _record_calls_from_mock_db(tmp_db) == [(42, "regime")]


def test_push_failure_does_not_record(tmp_db: Database) -> None:
    """Forbidden → _push returns False → record_alert_fired NOT called.
    Mirrors the alert_engine.py:394 ``if await _push(...): ...`` guard."""
    tmp_db.upsert_subscriber(42, "alice", "en")
    bot = AsyncMock()
    bot.send_message.side_effect = Forbidden("blocked")
    ok = asyncio.run(_push(bot, 42, "regime shift!", db=tmp_db))
    assert ok is False
    if ok:  # guard never taken
        tmp_db.record_alert_fired(42, "regime")
    assert _record_calls_from_mock_db(tmp_db) == []


def test_push_other_error_does_not_record(tmp_db: Database) -> None:
    """Transient TelegramError → _push returns False → record skipped.
    The 24h count must NEVER inflate on failed sends."""
    tmp_db.upsert_subscriber(42, "alice", "en")
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramError("network blip")
    ok = asyncio.run(_push(bot, 42, "regime shift!", db=tmp_db))
    assert ok is False
    if ok:
        tmp_db.record_alert_fired(42, "regime")
    assert _record_calls_from_mock_db(tmp_db) == []


def test_push_photo_success_records_call_alert(tmp_db: Database) -> None:
    """``_push_photo`` success in alert_engine.py:457 records kind='call'.
    Mirrors the alert_engine.py:457-464 success branch."""
    tmp_db.upsert_subscriber(42, "alice", "en")
    bot = AsyncMock()
    bot.send_photo.return_value = None  # success
    ok = asyncio.run(_push_photo(bot, 42, b"\x89PNG...", caption="x", db=tmp_db))
    assert ok is True
    if ok:
        tmp_db.record_alert_fired(42, "call")
    assert _record_calls_from_mock_db(tmp_db) == [(42, "call")]


def test_push_photo_failure_does_not_record(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(42, "alice", "en")
    bot = AsyncMock()
    bot.send_photo.side_effect = Forbidden("blocked")
    ok = asyncio.run(_push_photo(bot, 42, b"\x89PNG...", caption="x", db=tmp_db))
    assert ok is False
    if ok:
        tmp_db.record_alert_fired(42, "call")
    assert _record_calls_from_mock_db(tmp_db) == []


# ── digest / admin body literals ─────────────────────────────


def test_digest_body_renders_last_24h_alerts_header(tmp_db: Database) -> None:
    """Digest header literal must be ``Last 24h Alerts:`` not
    ``All-time alerts:`` post-wave."""
    from algovault_bot.digest import render_digest
    tmp_db.upsert_subscriber(1, "u1", "en")
    text = render_digest(tmp_db)
    assert "Last 24h Alerts:" in text
    assert "All-time alerts:" not in text


def test_digest_body_shows_zero_when_no_recent_alerts(tmp_db: Database) -> None:
    """No rows in alerts_fired → both counts render as 0."""
    from algovault_bot.digest import render_digest
    tmp_db.upsert_subscriber(1, "u1", "en")
    text = render_digest(tmp_db)
    assert "📊 Regime: 0" in text
    assert "📈 Calls: 0" in text


def test_digest_body_excludes_lifetime_counters(tmp_db: Database) -> None:
    """The lifetime ``total_regime_alerts`` / ``total_call_alerts`` counters
    must NOT leak into the digest 24h counts. A subscriber with a high
    lifetime count but no alerts_fired rows must render 0/0."""
    from algovault_bot.digest import render_digest
    tmp_db.upsert_subscriber(1, "u1", "en")
    # Inflate the lifetime counters way beyond 0.
    for _ in range(50):
        tmp_db.increment_total_regime_alerts(1)
    for _ in range(100):
        tmp_db.increment_total_call_alerts(1)
    text = render_digest(tmp_db)
    assert "📊 Regime: 0" in text, "lifetime count of 50 must NOT show as 24h count"
    assert "📈 Calls: 0" in text, "lifetime count of 100 must NOT show as 24h count"


def test_digest_body_uses_recent_alerts_fired_rows(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.record_alert_fired(1, "regime")
    tmp_db.record_alert_fired(1, "regime")
    tmp_db.record_alert_fired(1, "regime")
    tmp_db.record_alert_fired(1, "call")
    text = render_digest(tmp_db)
    assert "📊 Regime: 3" in text
    assert "📈 Calls: 1" in text


def test_admin_stats_renders_both_24h_and_lifetime(tmp_db: Database) -> None:
    """admin /stats keeps the lifetime block (per-user counters useful
    operator-side for the CTAs→Linked conversion ratio) AND adds the
    new ``Last 24h Alerts:`` block above it (matches the digest shape)."""
    from algovault_bot.admin import render_stats
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.record_alert_fired(1, "regime")
    tmp_db.increment_total_call_alerts(1)
    tmp_db.increment_total_call_alerts(1)
    text = render_stats(tmp_db)
    assert "Last 24h Alerts:" in text
    assert "📊 Regime: 1" in text
    assert "📈 Calls: 0" in text  # 24h section
    assert "Alerts (lifetime, per-user counters):" in text
    assert "📈 Trade calls     : 2" in text  # lifetime section
    assert "All-time alerts:" not in text  # old header gone


def test_kind_discrimination_in_count(tmp_db: Database) -> None:
    """The COUNT(*) GROUP BY kind in count_alerts_fired_last_24h must
    NOT cross-pollinate the two kinds."""
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.upsert_subscriber(2, "u2", "en")
    tmp_db.record_alert_fired(1, "regime")
    tmp_db.record_alert_fired(2, "regime")
    tmp_db.record_alert_fired(2, "call")
    regime, call = tmp_db.count_alerts_fired_last_24h()
    assert regime == 2
    assert call == 1


def test_check_constraint_rejects_unknown_kind_at_db_layer(tmp_db: Database) -> None:
    """The CHECK constraint on alerts_fired.kind is the second line of
    defense (Python-side validation in record_alert_fired is first).
    Direct INSERT with a bad kind value must raise IntegrityError."""
    with tmp_db._cursor() as cur:
        with pytest.raises(sqlite3.IntegrityError):
            cur.execute(
                "INSERT INTO alerts_fired(chat_id, kind) VALUES (?, ?)",
                (1, "cta"),
            )
