"""GROWTH-TG-FREE-ALLOWANCE-W1 R2.3 / R2.4 — a free window keeps the allowance it OPENED with.

Ruling R-1: lowering the Telegram allowance (200 → 100) walls nobody mid-window. Each 30-day free
window is stamped with the live allowance by the charge that opens it (`consume_quota_atomic`,
same COALESCE as `alerts_window_start`), the roll clears the stamp, and while the window is open
the chat is served max(stamp, live) — `quota.effective_free_total`, the ONE derivation the wall,
the 75 % / 90 % thresholds, the "used all N" notices and the charge's headroom all project from.

R2.4 (ruling Q1 = A): `db.upsert_free_tier_ladder` stamps every open, UNSTAMPED window with the
mirror's value BEFORE it replaces it, in one transaction — so the mirror never moves under an
unstamped open window, in any deploy order.

Chat ids are synthetic (≤ 7 digits), per the public-repo literal gate.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from algovault_bot import quota
from algovault_bot.cta import quota_threshold
from algovault_bot.db import Database
from algovault_bot.quota import (
    FREE_TIER_DAILY_QUOTA,
    WINDOW,
    consume_quota,
    effective_free_total,
    evaluate_delivery,
    get_quota_state,
)

OLD, NEW = 200, 100  # the cut this wave makes — the property holds for any OLD > NEW


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "t.db"))


def _mirror(db: Database, monthly: int, *, at: datetime | None = None) -> object:
    return db.upsert_free_tier_ladder(
        monthly, FREE_TIER_DAILY_QUOTA, None, None, (at or _now()).isoformat()
    )


def _row(db: Database, chat_id: int):
    return db.get_subscriber(chat_id)


def _set_window(db: Database, chat_id: int, *, used: int, start: datetime, cap: int | None) -> None:
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count = ?, alerts_window_start = ?, "
            "alerts_window_cap = ? WHERE chat_id = ?",
            (used, start.isoformat(), cap, chat_id),
        )


def _spread(db: Database, chat_id: int, units: int) -> None:
    """Charge `units` across UTC days so the DAILY cap cannot be what refuses."""
    left = units
    while left > 0:
        step = min(left, FREE_TIER_DAILY_QUOTA - 1)
        consume_quota(db, chat_id, step)
        with db._cursor() as cur:  # yesterday's count is not today's — the daily roll's only input
            cur.execute("UPDATE subscribers SET alerts_day = '2000-01-01' WHERE chat_id = ?", (chat_id,))
        left -= step


# ── the one derivation ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "live,cap,open_,expected",
    [
        (NEW, OLD, True, OLD),     # grandfathered: a cut never claws back an open window
        (OLD, NEW, True, OLD),     # a raise applies at once
        (NEW, NEW, True, NEW),
        (NEW, None, True, NEW),    # unstamped → live
        (NEW, OLD, False, NEW),    # a closed window is served the live ladder
        (NEW, 0, True, NEW),       # zero is not an allowance
        (NEW, True, True, NEW),    # bool is an int subclass, never a stamp of 1
    ],
)
def test_effective_free_total(live, cap, open_, expected) -> None:
    assert effective_free_total(live, cap, open_) == expected


# ── R2.3: the stamp, the grandfather, the new window, the roll, the raise ─────────────────────


def test_the_first_charge_of_a_window_stamps_the_LIVE_allowance(db: Database) -> None:
    _mirror(db, NEW)
    db.upsert_subscriber(11, "u", "en")
    consume_quota(db, 11, 1)
    assert _row(db, 11)["alerts_window_cap"] == NEW
    consume_quota(db, 11, 1)
    assert _row(db, 11)["alerts_window_cap"] == NEW, "a later charge never overwrites the stamp"


def test_a_grandfathered_window_is_served_at_150_and_walled_at_200(db: Database) -> None:
    _mirror(db, NEW)
    db.upsert_subscriber(12, "u", "en")
    _set_window(db, 12, used=150, start=_now() - timedelta(days=3), cap=OLD)
    st = get_quota_state(db, 12)
    assert (st.total, st.free_monthly_live, st.window_cap) == (OLD, NEW, OLD)
    assert st.exhausted is False, "150 of a 200 window is not a wall, whatever the live ladder says"
    assert evaluate_delivery(db, 12).allowed is True
    _set_window(db, 12, used=OLD, start=_now() - timedelta(days=3), cap=OLD)
    st = get_quota_state(db, 12)
    assert st.monthly_exhausted is True and st.limit_kind == "monthly"


def test_a_new_window_stamps_the_new_allowance_and_walls_at_it(db: Database) -> None:
    _mirror(db, NEW)
    db.upsert_subscriber(13, "u", "en")
    _spread(db, 13, NEW - 1)
    st = get_quota_state(db, 13)
    assert (st.total, st.window_cap) == (NEW, NEW) and st.exhausted is False
    _spread(db, 13, 1)
    st = get_quota_state(db, 13)
    assert st.used == NEW and st.monthly_exhausted is True


def test_the_roll_clears_the_stamp_so_the_next_window_opens_at_the_live_allowance(db: Database) -> None:
    _mirror(db, NEW)
    db.upsert_subscriber(14, "u", "en")
    _set_window(db, 14, used=OLD, start=_now() - WINDOW - timedelta(minutes=1), cap=OLD)
    st = get_quota_state(db, 14)
    assert (st.used, st.total, st.window_start, st.window_cap) == (0, NEW, None, None)
    assert _row(db, 14)["alerts_window_cap"] is None, "the roll is persisted, stamp included"
    consume_quota(db, 14, 1)
    assert _row(db, 14)["alerts_window_cap"] == NEW


def test_a_dry_run_read_computes_the_roll_without_writing_it(db: Database) -> None:
    _mirror(db, NEW)
    db.upsert_subscriber(15, "u", "en")
    _set_window(db, 15, used=OLD, start=_now() - WINDOW - timedelta(minutes=1), cap=OLD)
    st = get_quota_state(db, 15, persist_roll=False)
    assert (st.total, st.window_cap) == (NEW, None)
    assert _row(db, 15)["alerts_window_cap"] == OLD, "persist_roll=False must not write"


def test_a_raise_applies_at_once_inside_an_open_window(db: Database) -> None:
    _mirror(db, OLD)
    db.upsert_subscriber(16, "u", "en")
    _set_window(db, 16, used=NEW, start=_now() - timedelta(days=2), cap=NEW)
    st = get_quota_state(db, 16)
    assert st.total == OLD and st.exhausted is False


def test_the_thresholds_read_the_EFFECTIVE_total(db: Database) -> None:
    """75 % / 90 % of the window's allowance, not of the live ladder: at 100 used, a grandfathered
    200-window is at 50 % — no nudge — although 100 is 100 % of the live figure."""
    _mirror(db, NEW)
    db.upsert_subscriber(17, "u", "en")
    for used, bucket in ((NEW, None), (150, "75"), (180, "90"), (OLD, "100")):
        _set_window(db, 17, used=used, start=_now() - timedelta(days=1), cap=OLD)
        assert quota_threshold(get_quota_state(db, 17)) == bucket, used


def test_the_charge_caps_a_bonus_overflow_at_the_EFFECTIVE_total(db: Database) -> None:
    """The headroom arm of the atomic charge reads `monthly_total` = the effective total, so a
    referral-bonus overflow starts at the window's allowance, not at the live ladder."""
    _mirror(db, NEW)
    db.upsert_subscriber(18, "u", "en")
    _set_window(db, 18, used=150, start=_now() - timedelta(days=1), cap=OLD)
    with db._cursor() as cur:
        cur.execute("UPDATE subscribers SET referral_bonus_remaining = 10 WHERE chat_id = 18")
    consume_quota(db, 18, 60)
    row = _row(db, 18)
    assert (row["alert_count"], row["referral_bonus_remaining"]) == (OLD, 0)


# ── R2.4: the mirror never moves under an unstamped open window ──────────────────────────────────


def test_the_mirror_write_stamps_open_windows_with_the_allowance_they_opened_under(db: Database) -> None:
    """The cutover, end to end: a legacy window (opened before the column existed, so NULL) is
    stamped with the PRE-update mirror value in the same transaction that moves the mirror."""
    _mirror(db, OLD)
    db.upsert_subscriber(21, "u", "en")
    db.upsert_subscriber(22, "u", "en")
    db.upsert_subscriber(23, "u", "en")
    db.upsert_subscriber(24, "u", "en")
    _set_window(db, 21, used=150, start=_now() - timedelta(days=5), cap=None)      # open, unstamped
    _set_window(db, 22, used=40, start=_now() - timedelta(days=29), cap=None)      # open, unstamped
    _set_window(db, 23, used=10, start=_now() - timedelta(days=31), cap=None)      # expired → untouched
    _set_window(db, 24, used=10, start=_now() - timedelta(days=1), cap=NEW)        # already stamped
    stamp = _mirror(db, NEW)
    assert stamp == (OLD, 2, 2)
    assert [_row(db, c)["alerts_window_cap"] for c in (21, 22, 23, 24)] == [OLD, OLD, None, NEW]
    assert quota.resolve_ladder(db).free_monthly == NEW, "and only then the mirror moved"
    st = get_quota_state(db, 21)
    assert (st.total, st.exhausted) == (OLD, False), "the 150-alert chat is NOT walled mid-window"


def test_the_stamp_is_idempotent_and_inert_at_steady_state(db: Database) -> None:
    _mirror(db, OLD)
    db.upsert_subscriber(25, "u", "en")
    _set_window(db, 25, used=150, start=_now() - timedelta(days=5), cap=None)
    assert _mirror(db, NEW) == (OLD, 1, 1)
    assert _mirror(db, NEW) == (NEW, 0, 0), "nothing left unstamped: the leg stamps nothing"
    assert _row(db, 25)["alerts_window_cap"] == OLD


def test_no_mirror_row_means_nothing_to_stamp_from_and_it_is_logged(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    db.upsert_subscriber(26, "u", "en")
    _set_window(db, 26, used=5, start=_now() - timedelta(days=1), cap=None)
    with caplog.at_level(logging.WARNING, logger="algovault_bot.db"):
        stamp = _mirror(db, NEW)
    assert stamp == (None, 1, 0)
    assert _row(db, 26)["alerts_window_cap"] is None
    events = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    assert {"event": "ladder_window_caps_skipped", "reason": "no_mirror_row", "open_unstamped": 1} in events


def test_the_stamp_event_is_logged_once_with_no_chat_ids(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    _mirror(db, OLD)
    db.upsert_subscriber(27, "u", "en")
    _set_window(db, 27, used=5, start=_now() - timedelta(days=1), cap=None)
    caplog.clear()  # the setup's first-ever mirror write logged its own one-time "no row" skip
    with caplog.at_level(logging.WARNING, logger="algovault_bot.db"):
        _mirror(db, NEW)
        _mirror(db, NEW)  # steady state: silent
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == [json.dumps({"event": "ladder_window_caps_stamped", "cap": OLD, "expected": 1, "stamped": 1})]
    assert "27" not in msgs[0] and "chat" not in msgs[0]


def test_a_failed_mirror_write_rolls_the_stamps_back(db: Database, monkeypatch) -> None:
    """One transaction: a stamp that landed without its mirror move would freeze a window at an
    allowance the mirror never stopped serving — harmless here, but not the contract."""
    _mirror(db, OLD)
    db.upsert_subscriber(28, "u", "en")
    _set_window(db, 28, used=5, start=_now() - timedelta(days=1), cap=None)

    def _boom(cur, values):
        raise RuntimeError("write failed")

    monkeypatch.setattr(Database, "_write_free_tier_ladder", staticmethod(_boom))
    with pytest.raises(RuntimeError):
        _mirror(db, NEW)
    assert _row(db, 28)["alerts_window_cap"] is None
    assert quota.resolve_ladder(db).free_monthly == OLD
