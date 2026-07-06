"""OPS-DIGEST-TGBOT-METRIC-BRIDGE-W1 — the digest metrics snapshot + the shared-Postgres
bridge. Proves single-derivation (the bot_daily_metrics upsert params == the numbers the
bot's own digest renders) + the fail-soft/redaction contract on the write path.
"""
from __future__ import annotations

import logging

import pytest

from algovault_bot import digest
from algovault_bot.db import Database


def _seed(db: Database) -> None:
    # 3 subscribers; #3 has blocked the bot (excluded from Total / New / watch_total).
    db.upsert_subscriber(1, "alice", "en")
    db.upsert_subscriber(2, "bob", "en")
    db.upsert_subscriber(3, "carol", "en")
    with db._cursor() as cur:
        cur.execute("UPDATE subscribers SET bot_blocked_at = datetime('now') WHERE chat_id = 3")
    # 24h calls: 2 watch + 1 scanwatch + 0 scan = 3; plus 1 regime.
    db.record_alert_fired(1, "call", "watch")
    db.record_alert_fired(1, "call", "watch")
    db.record_alert_fired(1, "call", "scanwatch")
    db.record_alert_fired(2, "regime", "watch")
    # watchlists: 2 for reachable #1, 1 for blocked #3 (excluded) → watch_total == 2.
    db.add_watch(1, "BTC", "1h", "binance", "both")
    db.add_watch(1, "ETH", "4h", "binance", "calls")
    db.add_watch(3, "SOL", "1h", "binance", "calls")
    # 2 quota-exhausted notices.
    db.record_quota_notice_fired(1)
    db.record_quota_notice_fired(1)


def test_compute_metrics_numbers(tmp_db: Database) -> None:
    _seed(tmp_db)
    m = digest.compute_digest_metrics(tmp_db)
    assert m.total_subs == 2
    assert m.new_subs_24h == 2
    assert m.blocked == 1
    assert m.regime_24h == 1
    assert (m.calls_watch, m.calls_scanwatch, m.calls_scan) == (2, 1, 0)
    assert m.calls_24h == 3
    assert m.watch_total == 2
    assert m.quota_notices_24h == 2


def test_single_derivation_row_equals_rendered(tmp_db: Database) -> None:
    """The upsert row and the Telegram string derive from the SAME snapshot → they
    can never disagree (the whole point of the bridge)."""
    _seed(tmp_db)
    m = digest.compute_digest_metrics(tmp_db)
    sql, params = digest._bot_metrics_upsert(m)
    # param order MUST match _BOT_METRICS_UPSERT_SQL's column list
    assert params == (m.metric_date, 3, 2, 1, 0, 1, 2, 2, 1, 2, 2)
    rendered = digest._format_digest(m)
    # the row's numbers appear verbatim in the string rendered from the same m
    assert f"📈 Calls: {params[1]}" in rendered
    assert f"👁 Watch {params[2]}" in rendered
    assert f"🔭 Scanwatch {params[3]}" in rendered
    assert f"🔎 Scan {params[4]}" in rendered
    assert f"Total Subscribers: {params[6]}" in rendered
    assert f"📝 Watchlist entries: {params[9]}" in rendered
    assert f"Quota-exhausted notices: {params[10]}" in rendered


def test_render_digest_interface_preserved(tmp_db: Database) -> None:
    """render_digest(db) still works and equals _format_digest(compute(db)) up to the
    per-call timestamp (numbers identical)."""
    _seed(tmp_db)
    body = digest.render_digest(tmp_db)
    assert "🤖 Algovault-Telegram-bot — Daily Digest" in body
    assert "📈 Calls: 3  (👁 Watch 2 · 🔭 Scanwatch 1 · 🔎 Scan 0)" in body


def test_upsert_sql_column_param_arity() -> None:
    """The VALUES placeholder count (excluding the literal now()) matches the params."""
    m = digest.DigestMetrics(
        metric_date="2026-07-06", total_subs=0, new_subs_24h=0, blocked=0, regime_24h=0,
        calls_watch=0, calls_scanwatch=0, calls_scan=0, calls_24h=0, watch_total=0,
        quota_notices_24h=0, generated_at="2026-07-06T03:00:00+00:00",
    )
    sql, params = digest._bot_metrics_upsert(m)
    assert sql.count("%s") == len(params)  # 11 bound params; generated_at = now() literal


def test_write_fail_soft_when_dsn_unset(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIGNAL_PG_DSN", raising=False)
    m = digest.compute_digest_metrics(tmp_db)
    digest.write_bot_daily_metrics(m)  # must NOT raise (fail-soft skip)


def test_write_fail_soft_and_redacts_password(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sup3rSecretPw123"
    dsn = f"postgresql://algovault_bot_writer:{secret}@127.0.0.1:5432/signal_performance"
    monkeypatch.setenv("SIGNAL_PG_DSN", dsn)
    import psycopg

    def _boom(*a: object, **k: object):
        raise psycopg.OperationalError(f"connection failed for {dsn}")

    monkeypatch.setattr(psycopg, "connect", _boom)
    m = digest.compute_digest_metrics(tmp_db)
    with caplog.at_level(logging.ERROR):
        digest.write_bot_daily_metrics(m)  # fail-soft — no raise
    assert "fail-soft" in caplog.text
    assert secret not in caplog.text  # bare password redacted
    assert dsn not in caplog.text     # full DSN redacted


def test_redact_scrubs_dsn_and_password() -> None:
    dsn = "postgresql://u:pw_ABC@127.0.0.1:5432/db"
    out = digest._redact(f"boom {dsn} tail pw_ABC", dsn)
    assert "pw_ABC" not in out
    assert dsn not in out
