"""The wall-follow-up cadence. GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH2 R10.

This is a sender that DMs real people on a cron, so the tests that matter are the ones asserting
it STAYS SILENT. Nine of the cases below are silences; three are sends.

The measured context, which is why the shape is what it is: a free user walled on the monthly
meter was told once and then heard nothing for up to 27 days. Blocks are provably not caused by
the wall — 0 of 26 blocked users had ever been walled — so a bounded cadence is safe; what has
to be managed is nagging, and it is bounded five ways, each with a case here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from algovault_bot import entitlement_drain as ed
from algovault_bot.db import Database
from algovault_bot.notices import WALL_FOLLOWUP_3D, WALL_FOLLOWUP_7D
from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


class _Sends(list):
    """Every DM the pass would put on the wire. `ok` flips delivery for the failure case."""

    ok = True


@pytest.fixture()
def sends(monkeypatch: pytest.MonkeyPatch) -> _Sends:
    captured = _Sends()

    def _fake(chat_id, text, db_path=None, reply_markup=None, **kw):
        captured.append((chat_id, text, reply_markup))
        return captured.ok

    import algovault_bot.broadcast as broadcast

    monkeypatch.setattr(broadcast, "sendDM", _fake)
    # ARMED in every test by default, so a silence below is the CADENCE's decision and never the
    # flag's. The flag's own default is asserted on its own, in its own case.
    monkeypatch.setenv(ed.WALL_FOLLOWUPS_FLAG, "1")
    return captured


def _walled(db: Database, chat_id: int, *, days_ago: float, watches: int = 1) -> None:
    """A free chat, walled on the MONTHLY meter `days_ago` days back, inside a live window."""
    db.upsert_subscriber(chat_id, "u", "en")
    walled_at = (NOW - timedelta(days=days_ago)).isoformat()
    window_start = (NOW - timedelta(days=days_ago + 1)).isoformat()
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=?, "
            "quota_100_last_fired_at=? WHERE chat_id=?",
            (FREE_TIER_MONTHLY_QUOTA, window_start, walled_at, chat_id),
        )
    for i in range(watches):
        db.add_watch(chat_id, f"BTC{i}", "1h", "BINANCE", "calls")


def _run(db: Database, tmp_path, *, dry_run: bool = False):
    return ed.wall_followup_pass(db, str(tmp_path / "t.db"), now=NOW, dry_run=dry_run)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "t.db"))


# ── the silences ────────────────────────────────────────────────────────────────────────────


def test_two_days_walled_is_too_early(db, tmp_path, sends) -> None:
    _walled(db, 1, days_ago=2)
    counts, table = _run(db, tmp_path)
    assert sends == []
    assert counts["wall_followup_candidates"] == 0, "the SQL bound excludes it before any work"


def test_a_paid_chat_is_never_followed_up(db, tmp_path, sends) -> None:
    _walled(db, 1, days_ago=5)
    with db._cursor() as cur:
        cur.execute("UPDATE subscribers SET linked_api_key='k' WHERE chat_id=1")
    counts, _ = _run(db, tmp_path)
    assert sends == [] and counts["wall_followup_candidates"] == 0


def test_a_blocked_chat_is_never_followed_up(db, tmp_path, sends) -> None:
    _walled(db, 1, days_ago=5)
    db.mark_subscriber_blocked(1, NOW.isoformat())
    counts, _ = _run(db, tmp_path)
    assert sends == [] and counts["wall_followup_candidates"] == 0


def test_a_chat_with_nothing_to_resume_is_a_nag_not_a_service(db, tmp_path, sends) -> None:
    _walled(db, 1, days_ago=5, watches=0)
    counts, table = _run(db, tmp_path)
    assert sends == []
    assert table[0]["skip_reason"] == "no_watches"


def test_a_rolled_window_ends_the_episode(db, tmp_path, sends) -> None:
    """Once the meter resets the user is served again — there is nothing to follow up ON."""
    _walled(db, 1, days_ago=5)
    with db._cursor() as cur:
        cur.execute("UPDATE subscribers SET alert_count=0 WHERE chat_id=1")
    counts, table = _run(db, tmp_path)
    assert sends == []
    assert table[0]["skip_reason"] == "not_walled"


def test_the_second_cycle_sends_nothing(db, tmp_path, sends) -> None:
    """Idempotency per episode — the property the whole ledger exists for."""
    _walled(db, 1, days_ago=4)
    _run(db, tmp_path)
    assert len(sends) == 1
    _run(db, tmp_path)
    assert len(sends) == 1, "a second cycle must not re-send"


def test_the_flag_defaults_OFF_and_only_a_literal_1_arms_it(
    db, tmp_path, sends, monkeypatch
) -> None:
    """A user-facing sender ships behind ONE default-OFF flag. A typo must not arm it."""
    _walled(db, 1, days_ago=4)
    for value in ("", "0", "true", "yes", "TRUE", " ", "anything"):
        monkeypatch.setenv(ed.WALL_FOLLOWUPS_FLAG, value)
        counts, table = _run(db, tmp_path)
        assert sends == [], f"{value!r} must not arm the sender"
        assert counts["wall_followup_flag_off"] == 1
        # …and the TABLE is still produced: that is how CH3 reads the cadence while it is dark.
        assert table[0]["would_send"] == WALL_FOLLOWUP_3D


def test_a_failed_send_records_nothing_and_retries_next_cycle(db, tmp_path, sends) -> None:
    """Stamp only after a DELIVERED send — a blocked or rate-limited chat must not burn its
    one touch of the episode."""
    _walled(db, 1, days_ago=4)
    sends.ok = False
    counts, _ = _run(db, tmp_path)
    assert counts["wall_followup_3d"] == 0
    assert not db.has_notice(1, WALL_FOLLOWUP_3D, _episode(db, 1))

    sends.ok = True
    counts, _ = _run(db, tmp_path)
    assert counts["wall_followup_3d"] == 1, "the retry lands on the next cycle"


def _episode(db: Database, chat_id: int) -> str:
    with db._cursor() as cur:
        row = cur.execute(
            "SELECT alerts_window_start FROM subscribers WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return str(row[0])


# ── the sends ───────────────────────────────────────────────────────────────────────────────


def test_three_days_walled_gets_the_d3_touch(db, tmp_path, sends) -> None:
    _walled(db, 1, days_ago=3.5)
    counts, _ = _run(db, tmp_path)
    assert counts["wall_followup_3d"] == 1 and counts["wall_followup_7d"] == 0
    chat_id, text, markup = sends[0]
    assert chat_id == 1
    assert "paused for 3 days" in text
    assert markup is not None, "the follow-up carries the plan picker"
    assert "algovault.com" not in text


def test_a_candidate_three_days_and_one_second_old_IS_selected(db, tmp_path, sends) -> None:
    """The comparator-format trap, pinned.

    `quota_100_last_fired_at` is written as a Python isoformat with a `+00:00` offset, while the
    house SQL idiom is SQLite's space-separated `datetime('now','-3 days')`. Comparing the two
    lexicographically drops exactly the chats that have JUST become due — measured on the live
    cohort it cut 7 candidates to 5, silently and in one direction.
    """
    _walled(db, 1, days_ago=3 + (1 / 86400))
    counts, _ = _run(db, tmp_path)
    assert counts["wall_followup_candidates"] == 1
    assert counts["wall_followup_3d"] == 1


def test_a_chat_first_seen_at_day_nine_gets_ONE_message_and_a_superseded_row(
    db, tmp_path, sends
) -> None:
    """The supersede rule. Both touches are due at once; sending two is the nagging this
    cadence is bounded to avoid, and 'superseded' records that we CHOSE not to."""
    _walled(db, 1, days_ago=9)
    counts, table = _run(db, tmp_path)

    assert len(sends) == 1, "ONE message, never two"
    assert counts["wall_followup_7d"] == 1
    assert counts["wall_followup_3d"] == 0
    assert counts["wall_followup_superseded"] == 1
    assert table[0]["would_send"] == WALL_FOLLOWUP_7D
    assert table[0]["would_supersede"] == [WALL_FOLLOWUP_3D]

    ep = _episode(db, 1)
    assert db.has_notice(1, WALL_FOLLOWUP_7D, ep) and db.has_notice(1, WALL_FOLLOWUP_3D, ep)
    assert "Still paused" in sends[0][1]


def test_the_cycle_cap_bounds_the_blast_radius(db, tmp_path, sends) -> None:
    for chat_id in range(1, 26):
        _walled(db, chat_id, days_ago=4)
    counts, _ = _run(db, tmp_path)
    assert len(sends) == ed.WALL_FOLLOWUP_MAX_PER_CYCLE == 20
    assert counts["wall_followup_candidates"] == 25, "the denominator sees all of them"


# ── the dry run ─────────────────────────────────────────────────────────────────────────────


def test_a_dry_run_sends_nothing_and_writes_nothing(db, tmp_path, sends) -> None:
    """The go-live artifact. It must describe the cadence WITHOUT changing it — including not
    rolling a window, because `episode_key` IS `alerts_window_start`."""
    _walled(db, 1, days_ago=9)
    before = _episode(db, 1)

    counts, table = _run(db, tmp_path, dry_run=True)

    assert sends == []
    assert counts["wall_followup_7d"] == 0 and counts["wall_followup_superseded"] == 0
    assert not db.has_notice(1, WALL_FOLLOWUP_7D, before)
    assert _episode(db, 1) == before, "a dry run must not touch alerts_window_start"
    # …and it still REPORTS what a live run would do
    assert table[0]["would_send"] == WALL_FOLLOWUP_7D
    assert table[0]["would_supersede"] == [WALL_FOLLOWUP_3D]


def test_the_denominator_is_reported_even_when_nothing_is_due(db, tmp_path, sends) -> None:
    """A run of zeroes with no candidate count is indistinguishable from a pass that never ran."""
    counts, table = _run(db, tmp_path)
    assert counts["wall_followup_candidates"] == 0
    assert set(counts) >= {
        "wall_followup_candidates", "wall_followup_3d", "wall_followup_7d",
        "wall_followup_superseded", "wall_followup_skipped", "wall_followup_flag_off",
    }
