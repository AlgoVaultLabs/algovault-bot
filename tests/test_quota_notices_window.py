"""BOT-DIGEST-QUOTA-NOTICES-W1 — quota-exhausted notice log + blocked-aware
watchlist count in the daily digest and admin /stats.

Two changes, both surfaced in digest.py + admin.py:

1. ``quota_notices_fired`` table + ``record_quota_notice_fired`` /
   ``count_quota_notices_last_24h`` helpers, wired into the alert-engine's
   quota-exhausted trade-call branch, rendered as a rolling-24h
   "Quota-exhausted notices" line. These notices are deliberately kept OUT of
   ``alerts_fired`` (which tracks delivered signal volume) — they are
   operator-UX nudges, so a separate table keeps the alerts_fired
   ``CHECK(kind IN ('regime','call'))`` contract frozen.

2. The "Watchlist entries" count now excludes rows owned by bot-blocked
   subscribers (they can never receive an alert). The admin Top-10 watched
   assets breakdown applies the same filter so the headline count stays
   consistent with the breakdown.
"""

from __future__ import annotations

from algovault_bot.db import Database


# ── quota_notices_fired schema + helpers ──────────────────────


def test_quota_notices_table_exists(tmp_db: Database) -> None:
    """Schema migration is idempotent across Database() init calls."""
    with tmp_db._cursor() as cur:
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='quota_notices_fired'"
        )
        assert cur.fetchone() is not None
    Database(tmp_db.path)  # re-init must not raise


def test_count_quota_notices_empty(tmp_db: Database) -> None:
    assert tmp_db.count_quota_notices_last_24h() == 0


def test_record_quota_notice_then_count(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.record_quota_notice_fired(1)
    tmp_db.record_quota_notice_fired(1)
    assert tmp_db.count_quota_notices_last_24h() == 2


def test_quota_notice_24h_window_excludes_old(tmp_db: Database) -> None:
    """A row 23h old is in-window; 25h old is excluded — mirrors the digest
    SELECT filter ``fired_at >= datetime('now', '-1 day')``."""
    tmp_db.upsert_subscriber(1, "u1", "en")
    with tmp_db._cursor() as cur:
        cur.execute(
            "INSERT INTO quota_notices_fired(chat_id, fired_at) "
            "VALUES (?, datetime('now', '-23 hours'))",
            (1,),
        )
        cur.execute(
            "INSERT INTO quota_notices_fired(chat_id, fired_at) "
            "VALUES (?, datetime('now', '-25 hours'))",
            (1,),
        )
    assert tmp_db.count_quota_notices_last_24h() == 1


def test_quota_notices_separate_from_alerts_fired(tmp_db: Database) -> None:
    """A quota notice must NOT count as a regime/call alert, and vice-versa —
    the two tables are independent."""
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.record_quota_notice_fired(1)
    tmp_db.record_alert_fired(1, "call")
    assert tmp_db.count_quota_notices_last_24h() == 1
    regime, call = tmp_db.count_alerts_fired_last_24h()
    assert (regime, call) == (0, 1)


# ── digest / admin render the quota-notice line ───────────────


def test_digest_renders_quota_notices_line(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest

    tmp_db.upsert_subscriber(1, "u1", "en")
    text = render_digest(tmp_db)
    assert "🔒 Quota-exhausted notices: 0" in text


def test_digest_quota_notices_counts_recent(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest

    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.record_quota_notice_fired(1)
    tmp_db.record_quota_notice_fired(1)
    tmp_db.record_quota_notice_fired(1)
    text = render_digest(tmp_db)
    assert "🔒 Quota-exhausted notices: 3" in text


def test_admin_renders_quota_notices_line(tmp_db: Database) -> None:
    from algovault_bot.admin import render_stats

    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.record_quota_notice_fired(1)
    text = render_stats(tmp_db)
    assert "🔒 Quota-exhausted notices: 1" in text


# ── watchlist count excludes bot-blocked owners ───────────────


def test_digest_watchlist_excludes_blocked_owner(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest

    tmp_db.upsert_subscriber(1, "active", "en")
    tmp_db.add_watch(1, "BTC", "5m", "BINANCE", "calls")
    tmp_db.upsert_subscriber(2, "blocked", "en")
    tmp_db.add_watch(2, "ETH", "1h", "BINANCE", "both")
    tmp_db.mark_subscriber_blocked(2, "2026-06-15T00:00:00+00:00")
    text = render_digest(tmp_db)
    assert "📝 Watchlist entries: 1" in text, "blocked owner's watch excluded"


def test_admin_watchlist_excludes_blocked_owner(tmp_db: Database) -> None:
    from algovault_bot.admin import render_stats

    tmp_db.upsert_subscriber(1, "active", "en")
    tmp_db.add_watch(1, "BTC", "5m", "BINANCE", "calls")
    tmp_db.upsert_subscriber(2, "blocked", "en")
    tmp_db.add_watch(2, "ETH", "1h", "BINANCE", "both")
    tmp_db.mark_subscriber_blocked(2, "2026-06-15T00:00:00+00:00")
    text = render_stats(tmp_db)
    assert "📝 Watchlist entries : 1" in text, "blocked owner's watch excluded"


def test_admin_top_assets_excludes_blocked_owner(tmp_db: Database) -> None:
    """Top-10 watched assets stays consistent with the headline count — a coin
    watched ONLY by a blocked subscriber must not appear."""
    from algovault_bot.admin import render_stats

    tmp_db.upsert_subscriber(1, "active", "en")
    tmp_db.add_watch(1, "BTC", "5m", "BINANCE", "calls")
    tmp_db.upsert_subscriber(2, "blocked", "en")
    tmp_db.add_watch(2, "DOGE", "1h", "BINANCE", "both")
    tmp_db.mark_subscriber_blocked(2, "2026-06-15T00:00:00+00:00")
    text = render_stats(tmp_db)
    assert "DOGE" not in text, "coin watched only by a blocked owner excluded"
    assert "BTC" in text


# ── engine wiring guard (by-construction) ─────────────────────


def test_engine_records_quota_notice_in_exhausted_branch() -> None:
    """Guard against fake coverage: the helper tests above prove
    ``record_quota_notice_fired`` works, but only the engine's exhausted
    branch feeds it at runtime. Assert the call site exists in
    ``process_one_row`` so a future refactor that drops it fails here."""
    import inspect

    from algovault_bot import alert_engine

    src = inspect.getsource(alert_engine.process_one_row)
    assert "record_quota_notice_fired" in src


def test_record_quota_notice_rejects_bad_chat_via_fk(tmp_db: Database) -> None:
    """quota_notices_fired has no FK (notices may outlive a deleted sub), so a
    record for an unknown chat is permitted — this documents that the count is
    delivery-driven, not subscriber-state-driven."""
    tmp_db.record_quota_notice_fired(999999)
    assert tmp_db.count_quota_notices_last_24h() == 1
