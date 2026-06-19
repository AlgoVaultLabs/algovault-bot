"""TG-WATCH-ADOPTION-BROADCAST-W1 (R1): 0-engagement segment query + first-watch
nudge dedupe."""
from __future__ import annotations

from algovault_bot import handlers


def _sub(db, chat_id):
    db.upsert_subscriber(chat_id, f"u{chat_id}", "en")


def test_zero_engagement_segment_excludes_watchers_and_scanners(tmp_db):
    # 1: no engagement (target). 2: has a watch. 3: has a scan_watch. 4: no engagement.
    for cid in (1, 2, 3, 4):
        _sub(tmp_db, cid)
    tmp_db.add_watch(2, "BTC", "1h", "BINANCE", "calls")
    tmp_db.add_scan_watch(3, 20, "15m", "BINANCE", "1h")

    segment = tmp_db.list_zero_engagement_unnudged()
    assert set(segment) == {1, 4}


def test_segment_excludes_blocked_and_already_nudged(tmp_db):
    for cid in (1, 2, 3):
        _sub(tmp_db, cid)
    tmp_db.mark_subscriber_blocked(2, "2026-06-19T00:00:00")  # blocked → unreachable
    tmp_db.mark_first_watch_nudge_sent(3, "2026-06-19T00:00:00")  # already nudged

    assert tmp_db.list_zero_engagement_unnudged() == [1]


def test_nudge_dedupe_flag_roundtrip(tmp_db):
    _sub(tmp_db, 1)
    assert tmp_db.get_first_watch_nudge_sent_at(1) is None
    assert handlers.should_send_first_watch_nudge(tmp_db, 1) is True

    tmp_db.mark_first_watch_nudge_sent(1, "2026-06-19T12:00:00")
    assert tmp_db.get_first_watch_nudge_sent_at(1) == "2026-06-19T12:00:00"
    # Deduped — never selected / never eligible again.
    assert handlers.should_send_first_watch_nudge(tmp_db, 1) is False
    assert 1 not in tmp_db.list_zero_engagement_unnudged()


def test_should_send_false_when_already_engaged(tmp_db):
    _sub(tmp_db, 1)
    tmp_db.add_watch(1, "ETH", "4h", "BINANCE", "calls")
    assert handlers.should_send_first_watch_nudge(tmp_db, 1) is False


def test_has_any_engagement(tmp_db):
    _sub(tmp_db, 1)
    assert tmp_db.has_any_engagement(1) is False
    tmp_db.add_watch(1, "BTC", "1h", "BINANCE", "calls")
    assert tmp_db.has_any_engagement(1) is True
