"""A dry run must not write. GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1 R2b.

WHY THIS IS ITS OWN FILE. `get_quota_state`'s docstring said "Auto-rolls expired window" and
`evaluate_delivery`'s said "Pure read — no writes, no network, safe to call on every cycle".
The second was FALSE: the roll executes `UPDATE subscribers SET alert_count = 0,
alerts_window_start = NULL`, so any caller that trusted the docstring and called it to INSPECT a
subscriber mutated the very column it was inspecting.

That matters more here than it would elsewhere. CH2's wall-follow-up pass walks every walled
chat and asks `evaluate_delivery` whether each is still walled — and its `--dry-run` mode is the
thing a human reads BEFORE the flag is flipped and real users are DM'd. A dry run that silently
rolls a window would change the population it is reporting on, and the operator would approve a
table that no longer describes reality.

`episode_key` is `alerts_window_start`, so a spurious roll also destroys the ledger key the
whole cadence is idempotent on.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from algovault_bot.db import Database
from algovault_bot.quota import (
    WINDOW,
    evaluate_delivery,
    get_quota_state,
)


def _expired_window(db: Database, chat_id: int) -> str:
    """A subscriber whose 30-day window closed yesterday — the only state that rolls."""
    stale = (datetime.now(timezone.utc) - WINDOW - timedelta(days=1)).isoformat()
    db.upsert_subscriber(chat_id, "u", "en")
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=?",
            (200, stale, chat_id),
        )
    return stale


def _stored_window(db: Database, chat_id: int) -> str | None:
    with db._cursor() as cur:
        row = cur.execute(
            "SELECT alerts_window_start FROM subscribers WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row[0]


def test_the_default_still_persists_the_roll(tmp_path) -> None:
    """Every existing call site is unchanged — that is what makes this keyword safe to add."""
    db = Database(str(tmp_path / "t.db"))
    _expired_window(db, 1)
    get_quota_state(db, 1)
    assert _stored_window(db, 1) is None, "the default must keep rolling, as it always has"


def test_persist_roll_false_returns_the_rolled_state_without_writing(tmp_path) -> None:
    """The returned state is IDENTICAL either way; only the write is suppressed.

    Both halves matter. If it did not roll in memory, the dry run would report a walled user who
    is actually served — the opposite error, and it would page for a follow-up nobody needs.
    """
    db = Database(str(tmp_path / "t.db"))
    stale = _expired_window(db, 1)

    state = get_quota_state(db, 1, persist_roll=False)

    assert state.used == 0 and state.window_start is None, "the roll is still COMPUTED"
    assert _stored_window(db, 1) == stale, "and NOT written"


def test_evaluate_delivery_forwards_it(tmp_path) -> None:
    """The decision is what the follow-up pass actually calls, so the seam has to reach it."""
    db = Database(str(tmp_path / "t.db"))
    stale = _expired_window(db, 1)

    decision = evaluate_delivery(db, 1, persist_roll=False)

    assert decision.allowed is True, "a rolled window serves"
    assert _stored_window(db, 1) == stale, "a dry run must leave the column exactly as found"


def test_a_live_window_is_untouched_by_either_mode(tmp_path) -> None:
    """The roll branch is the only writer; a healthy subscriber must see no difference at all."""
    db = Database(str(tmp_path / "t.db"))
    fresh = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    db.upsert_subscriber(2, "u", "en")
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=?",
            (5, fresh, 2),
        )
    for persist in (True, False):
        get_quota_state(db, 2, persist_roll=persist)
        assert _stored_window(db, 2) == fresh
