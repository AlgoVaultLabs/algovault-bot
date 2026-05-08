from __future__ import annotations

from algovault_bot.db import Database


def test_subscriber_upsert_idempotent(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(123, "alice", "en")
    tmp_db.upsert_subscriber(123, "alice2", "en")  # update path
    row = tmp_db.get_subscriber(123)
    assert row is not None
    assert row["username"] == "alice2"
    assert tmp_db.count_subscribers() == 1


def test_add_watch_then_list(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    inserted = tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "both")
    assert inserted is True
    rows = tmp_db.list_watches(1)
    assert len(rows) == 1
    assert rows[0]["coin"] == "BTC"
    assert rows[0]["alert_type"] == "both"


def test_add_watch_idempotent_updates_alert_type(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "regime")
    inserted = tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "calls")
    assert inserted is False
    rows = tmp_db.list_watches(1)
    assert len(rows) == 1
    assert rows[0]["alert_type"] == "calls"


def test_remove_watch(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "both")
    assert tmp_db.remove_watch(1, "BTC", "4h", "BINANCE") is True
    assert tmp_db.count_watches(1) == 0
    # Removing again returns False.
    assert tmp_db.remove_watch(1, "BTC", "4h", "BINANCE") is False


def test_count_watches_per_user(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.upsert_subscriber(2, "u2", "en")
    tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "both")
    tmp_db.add_watch(1, "ETH", "1h", "BINANCE", "both")
    tmp_db.add_watch(2, "BTC", "4h", "BINANCE", "both")
    assert tmp_db.count_watches(1) == 2
    assert tmp_db.count_watches(2) == 1


def test_due_watches_filter_by_age(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "both")  # last_fetched_at NULL → due
    from algovault_bot.validators import TF_SECONDS

    due = tmp_db.list_due_watches(1_700_000_000, TF_SECONDS)
    assert len(due) == 1
    assert due[0]["coin"] == "BTC"


def test_update_watch_after_fetch(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "both")
    tmp_db.update_watch_after_fetch(
        1, "BTC", "4h", "BINANCE", "TRENDING_UP", 2, "TRENDING_UP"
    )
    rows = tmp_db.list_watches(1)
    assert rows[0]["coin"] == "BTC"
    # last_fetched_at and last_verdict are written, but list_watches doesn't return them.
    # Use the cursor directly to validate.
    with tmp_db._cursor() as cur:
        cur.execute(
            "SELECT last_verdict, last_verdict_streak, regime_last_seen FROM watchlists "
            "WHERE chat_id=1 AND coin='BTC' AND timeframe='4h' AND exchange='BINANCE'"
        )
        r = cur.fetchone()
        assert r["last_verdict"] == "TRENDING_UP"
        assert r["last_verdict_streak"] == 2
        assert r["regime_last_seen"] == "TRENDING_UP"
