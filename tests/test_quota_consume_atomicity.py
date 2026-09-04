"""OPS-BOT-DISPATCH-LATENCY-W1 CH2 — the free meter's charge must not lose an update.

`consume_quota` was a read-modify-write: `get_quota_state` read `alert_count`, the bonus
arithmetic ran in Python, and an UPDATE wrote the ABSOLUTE result. Two charges interleaving on
one subscriber both read N and both wrote N+1 — one delivered alert billed to nobody.

This needs no concurrency inside the alert engine to happen. `record_call_delivered` is called
from the cron engine AND from `handlers.py` inside the separate, always-running
`algovault-bot.service`, and both open the same `/var/lib/algovault-bot/state.db`. A user
pressing a button while their watch tick fires is the whole reproduction.

THE INTERLEAVE IS KEYED ON `get_quota_state` — a call BOTH shapes make. That is the trap
`AOE-RETUNE-IDEMPOTENCY-W1` recorded from its own first attempt: a race test keyed on a marker
only the NEW shape emits can never fail against the OLD one, so it proves nothing. The seam
used here exists identically before and after the fix, which is why
`test_selftest_the_read_modify_write_shape_loses_an_update` can reconstruct the old behaviour
and watch the same assertions break.
"""

from __future__ import annotations

from typing import Any

import pytest

from algovault_bot import quota
from algovault_bot.db import Database
from algovault_bot.quota import consume_quota, get_quota_state


def _free(db: Database, chat_id: int = 1) -> None:
    db.upsert_subscriber(chat_id, "u", "en")


def _cols(db: Database, chat_id: int = 1) -> Any:
    with db._cursor() as cur:
        return cur.execute(
            "SELECT alert_count, referral_bonus_remaining, alerts_day_count, alerts_day, "
            "alerts_window_start FROM subscribers WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()


def _interleave_once(monkeypatch: pytest.MonkeyPatch, db: Database, chat_id: int,
                     units: int = 1) -> None:
    """Fire a COMPETING charge inside the first `get_quota_state`, i.e. exactly between the
    outer charge's read and its write. This is the concurrent second writer, made
    deterministic."""
    real = quota.get_quota_state
    fired = {"done": False}

    def patched(d: Database, cid: int):
        state = real(d, cid)
        if not fired["done"] and cid == chat_id:
            fired["done"] = True
            consume_quota(d, cid, units)   # the other process, mid-flight
        return state

    monkeypatch.setattr(quota, "get_quota_state", patched)


# ── the guarantee ──────────────────────────────────────────────────────────


def test_interleaved_charges_both_land(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two units delivered, two units billed. Under the read-modify-write shape this was 1."""
    _free(tmp_db)
    _interleave_once(monkeypatch, tmp_db, 1)

    consume_quota(tmp_db, 1)

    assert _cols(tmp_db)["alert_count"] == 2


def test_interleaved_charges_both_tick_the_daily_meter(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daily wall is a separate counter and was lost by the same mechanism."""
    _free(tmp_db)
    _interleave_once(monkeypatch, tmp_db, 1)

    consume_quota(tmp_db, 1)

    assert _cols(tmp_db)["alerts_day_count"] == 2


def test_interleaved_charges_do_not_lose_bonus_drawdown(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`referral_bonus_remaining` carries granted user value. Fixing only `alert_count` would
    close the lost-update class on the counter nobody was losing and leave it open on the one
    that costs a user something — so it is asserted separately, not assumed to follow."""
    _free(tmp_db)
    with tmp_db._cursor() as cur:
        # Monthly headroom exhausted, so every unit draws from the bonus pool.
        cur.execute(
            "UPDATE subscribers SET alert_count = 100000, referral_bonus_remaining = 10 "
            "WHERE chat_id = 1"
        )
    _interleave_once(monkeypatch, tmp_db, 1)

    consume_quota(tmp_db, 1)

    assert _cols(tmp_db)["referral_bonus_remaining"] == 8, "both drawdowns must land"


def test_two_first_charges_cannot_each_start_a_window(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`alerts_window_start` used to be set by an `if state.window_start is None` branch, so an
    interleave could restart the 30-day window and hand back a month of quota. COALESCE makes
    the decision inside the statement."""
    _free(tmp_db)
    _interleave_once(monkeypatch, tmp_db, 1)

    consume_quota(tmp_db, 1)

    row = _cols(tmp_db)
    assert row["alerts_window_start"] is not None
    # The window belongs to whichever charge got there first, and is never rewritten.
    inner_window = row["alerts_window_start"]
    consume_quota(tmp_db, 1)
    assert _cols(tmp_db)["alerts_window_start"] == inner_window


# ── semantics preserved (the SQL must mean what the Python meant) ───────────


def test_bonus_overflow_still_fills_monthly_headroom_first(tmp_db: Database) -> None:
    _free(tmp_db)
    total = get_quota_state(tmp_db, 1).total
    with tmp_db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count = ?, referral_bonus_remaining = 10 "
            "WHERE chat_id = 1",
            (total - 2,),
        )
    consume_quota(tmp_db, 1, units=5)

    row = _cols(tmp_db)
    assert row["alert_count"] == total, "2 units fill the headroom"
    assert row["referral_bonus_remaining"] == 7, "the other 3 draw from the pool"


def test_no_bonus_pool_means_the_monthly_meter_is_uncapped(tmp_db: Database) -> None:
    """With no pool the meter runs past `total` and the user is walled by the gate, not by the
    counter. Byte-identical to the pre-wave Python for the (today: 100%) bonus-free base."""
    _free(tmp_db)
    total = get_quota_state(tmp_db, 1).total
    with tmp_db._cursor() as cur:
        cur.execute("UPDATE subscribers SET alert_count = ? WHERE chat_id = 1", (total,))
    consume_quota(tmp_db, 1, units=3)

    assert _cols(tmp_db)["alert_count"] == total + 3


def test_a_stale_day_contributes_zero(tmp_db: Database) -> None:
    """The daily meter rolls on WRITE as well as on read, so a subscriber walled yesterday is
    served today with nothing having run overnight."""
    _free(tmp_db)
    with tmp_db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alerts_day_count = 99, alerts_day = '1999-01-01' "
            "WHERE chat_id = 1"
        )
    consume_quota(tmp_db, 1, units=2)

    assert _cols(tmp_db)["alerts_day_count"] == 2


def test_same_day_accumulates(tmp_db: Database) -> None:
    _free(tmp_db)
    consume_quota(tmp_db, 1, units=2)
    consume_quota(tmp_db, 1, units=3)
    assert _cols(tmp_db)["alerts_day_count"] == 5


def test_paid_tier_is_still_a_no_op(tmp_db: Database) -> None:
    _free(tmp_db, 2)
    with tmp_db._cursor() as cur:
        cur.execute("UPDATE subscribers SET linked_tier = 'starter' WHERE chat_id = 2")
    consume_quota(tmp_db, 2, units=5)
    assert _cols(tmp_db, 2)["alert_count"] == 0


def test_a_deleted_subscriber_does_not_raise(tmp_db: Database) -> None:
    """The row can vanish between the state read and the charge (a /stop mid-tick). The
    statement matches nothing, RETURNING yields None, and the alert path must not explode."""
    assert tmp_db.consume_quota_atomic(999_999, 1, 200, "2026-09-04", "2026-09-04T00:00:00") is None


# ── RED-verify: prove the suite discriminates ──────────────────────────────


def test_selftest_the_read_modify_write_shape_loses_an_update(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROOF THE ASSERTIONS ABOVE CAN FAIL. Reconstructs the pre-fix shape — read the counter,
    compute in Python, write the ABSOLUTE result — under the identical interleave, and asserts
    the increment IS lost.

    If this ever starts reporting 2, the interleave seam has stopped discriminating and every
    atomicity assertion in this file has gone vacuous.
    """
    _free(tmp_db)

    def read_modify_write(db: Database, chat_id: int, units: int = 1) -> None:
        state = get_quota_state(db, chat_id)          # READ
        new_used = state.used + units                 # modify, in Python
        with db._cursor() as cur:                     # absolute WRITE
            cur.execute(
                "UPDATE subscribers SET alert_count = ? WHERE chat_id = ?", (new_used, chat_id)
            )

    real = quota.get_quota_state
    fired = {"done": False}

    def patched(d: Database, cid: int):
        state = real(d, cid)
        if not fired["done"]:
            fired["done"] = True
            read_modify_write(d, cid, 1)
        return state

    monkeypatch.setattr(quota, "get_quota_state", patched)
    read_modify_write(tmp_db, 1, 1)

    assert _cols(tmp_db)["alert_count"] == 1, (
        "the pre-fix shape loses one of two interleaved charges — this is the defect"
    )
