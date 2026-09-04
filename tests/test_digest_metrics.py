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


def test_single_derivation_row_equals_rendered(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upsert row and the Telegram string derive from the SAME snapshot → they
    can never disagree (the whole point of the bridge)."""
    # Pin the provenance stamp to a path that cannot exist. Without this the expected tuple
    # would depend on whether the machine running the test happens to be a deploy host — a test
    # that passes on CI and fails on the box it describes.
    monkeypatch.setattr(digest, "DEPLOYED_SHA_PATH", "/nonexistent/DEPLOYED_SHA")
    _seed(tmp_db)
    m = digest.compute_digest_metrics(tmp_db)
    sql, params = digest._bot_metrics_upsert(m)
    # param order MUST match _BOT_METRICS_UPSERT_SQL's column list
    # OPS-DIGEST-TGBOT-TIER-AND-WALLED-W1 widened the row by three: calls_paid_linked,
    # walled_now, walled_silent. The seeded fixture has no linked_tier and no walled
    # subscriber, so all three are 0 — asserted explicitly rather than sliced off, since
    # a silently-truncated comparison is how a dropped bridge column would pass.
    # The trailing None is `deployed_sha` (CH3c): no stamp on this machine, so no provenance.
    assert params == (m.metric_date, 3, 2, 1, 0, 1, 2, 2, 1, 2, 2, 0, 0, 0, 0, 0, 0, None)
    assert (m.calls_paid_linked, m.walled_now, m.walled_silent) == (0, 0, 0)
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
        quota_notices_24h=0,
        # NOTE: this comment previously said `walled_now` was deliberately NOT bridged and that
        # "arity stays 11". Both statements stopped being true when PRICING-BOT-DELIVERY-
        # METERING-W1 CH5f/CH6e widened the bridge; the stale text survived because nothing
        # asserts on a comment. Current truth: the point-in-time walled state IS bridged (as
        # `walled_paid_now`) and the arity is asserted below rather than stated here.
        walled_now=0, walled_silent=0, walled_paid=0, calls_paid_linked=0,
        # OPS-VALIDATE-KEY-INDETERMINATE-W1 CH6 — the leak meter. DELIBERATELY NOT bridged to
        # postgres: `bot_daily_metrics` lives in signal-MCP's DB behind a least-privilege role,
        # so widening it is a cross-repo migration. These are TG-digest-only for now, which is
        # why the arity asserted below is UNCHANGED by this wave.
        unmetered_24h=0, linked_by_state={},
        plan_units_debited=0, outbox_pending=0, deployed_sha=None,
        generated_at="2026-07-06T03:00:00+00:00",
    )
    sql, params = digest._bot_metrics_upsert(m)
    assert sql.count("%s") == len(params)  # generated_at is a now() literal, not a param


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


# ---------------------------------------------------------------------------
# OPS-DEPLOY-PROVENANCE-AND-VERDICT-CLASS-W1 CH3c — deployed-commit provenance.
#
# The property under test is NOT "it reads a file". It is that every failure mode produces None
# rather than a plausible-looking value: a bot reporting a sha it is not running is strictly worse
# than a bot reporting nothing, because the first is believed.
# ---------------------------------------------------------------------------

SHA = "af995e5c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f60"


def test_deployed_sha_reads_the_host_stamp(tmp_path) -> None:
    p = tmp_path / "DEPLOYED_SHA"
    p.write_text(f"sha={SHA}\nshort=af995e5\nref=main\n", encoding="utf-8")
    assert digest.read_deployed_sha(str(p)) == SHA


def test_deployed_sha_is_none_when_the_stamp_is_absent(tmp_path) -> None:
    # A bot deployed before the stamp existed. Absent, not zero, not "unknown".
    assert digest.read_deployed_sha(str(tmp_path / "nope")) is None


def test_deployed_sha_is_none_when_the_file_is_unreadable(tmp_path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    assert digest.read_deployed_sha(str(d)) is None


@pytest.mark.parametrize(
    "body",
    [
        "sha=not-a-sha\n",                 # not hex
        "sha=af995e5\n",                   # short sha, not the full 40
        f"sha={SHA.upper()}\n",            # uppercase — one canonical form only
        f"sha={SHA}extra\n",               # too long
        "ref=main\nshort=af995e5\n",       # a stamp with everything BUT the sha
        "",                                # empty file
    ],
)
def test_a_malformed_stamp_is_none_never_passed_through(tmp_path, body: str) -> None:
    p = tmp_path / "DEPLOYED_SHA"
    p.write_text(body, encoding="utf-8")
    assert digest.read_deployed_sha(str(p)) is None


def test_deployed_sha_rides_the_bridge_and_keeps_arity(tmp_db: Database, tmp_path) -> None:
    # The whole point of the column: it must actually reach the upsert, and adding it must not
    # desync the placeholder/param counts the way a hand-widened INSERT usually does.
    p = tmp_path / "DEPLOYED_SHA"
    p.write_text(f"sha={SHA}\n", encoding="utf-8")
    _seed(tmp_db)
    m = digest.compute_digest_metrics(tmp_db)
    m = type(m)(**{**m.__dict__, "deployed_sha": digest.read_deployed_sha(str(p))})
    sql, params = digest._bot_metrics_upsert(m)
    assert sql.count("%s") == len(params)
    assert "deployed_sha" in sql
    assert SHA in params


def test_the_bridge_carries_none_when_there_is_no_provenance(tmp_db: Database) -> None:
    _seed(tmp_db)
    m = digest.compute_digest_metrics(tmp_db)
    m = type(m)(**{**m.__dict__, "deployed_sha": None})
    _sql, params = digest._bot_metrics_upsert(m)
    # None -> SQL NULL. Never "" and never "unknown": the canary distinguishes them.
    assert None in params
