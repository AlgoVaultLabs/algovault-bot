"""GROWTH-TG-QUOTA-PARITY-W1 CH2 — the free lane's DAILY meter, and the ladder it serves from.

The free lane metered monthly only. It now meters monthly AND daily, and a call is refused when
EITHER is spent. These tests pin the parts that are easy to get subtly wrong: the roll, the two
episode keys, and the fallback that must SERVE rather than refuse.

Every figure below is derived from `quota.FREE_TIER_*` rather than typed. A test that hard-types
the cap is the same defect as copy that hard-types it — it just fails later and more confusingly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from algovault_bot.db import Database
from algovault_bot.quota import (
    FREE_TIER_DAILY_QUOTA,
    FREE_TIER_MONTHLY_QUOTA,
    LADDER_STALE_AFTER,
    STARTER_MONTHLY_CALLS,
    STARTER_PRICE_USD,
    QuotaState,
    _notice_due,
    _utc_day_key,
    consume_quota,
    evaluate_delivery,
    get_quota_state,
    resolve_ladder,
)

M = FREE_TIER_MONTHLY_QUOTA
D = FREE_TIER_DAILY_QUOTA


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "t.db"))


def _set_day(db: Database, chat_id: int, day: str, count: int) -> None:
    """Force a subscriber's daily meter to a given (day, count) — the roll's only input."""
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alerts_day = ?, alerts_day_count = ? WHERE chat_id = ?",
            (day, count, chat_id),
        )


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


# ── the two walls ────────────────────────────────────────────────────────────────────────────


def test_monthly_wall_is_the_ladder_value(db: Database) -> None:
    db.upsert_subscriber(1, "u", "en")
    # Spread across days so the DAILY cap cannot be what refuses: this test is about the monthly.
    consume_quota(db, 1, M - 1)
    _set_day(db, 1, _utc_day_key(), 0)
    st = get_quota_state(db, 1)
    assert st.total == M
    assert st.monthly_exhausted is False
    consume_quota(db, 1, 1)
    _set_day(db, 1, _utc_day_key(), 0)
    st = get_quota_state(db, 1)
    assert st.used == M
    assert st.monthly_exhausted is True
    assert st.limit_kind == "monthly"


def test_daily_wall_binds_well_below_the_monthly_one(db: Database) -> None:
    """The whole point of the second meter: D alerts in a day refuses, with M-D still unspent."""
    db.upsert_subscriber(2, "u", "en")
    consume_quota(db, 2, D)
    st = get_quota_state(db, 2)
    assert st.day_used == D and st.day_total == D
    assert st.daily_exhausted is True
    assert st.monthly_exhausted is False, "the monthly lane still has headroom"
    assert st.exhausted is True, "EITHER meter refuses"
    assert st.limit_kind == "daily"


def test_monthly_wins_the_tie_when_both_are_spent(db: Database) -> None:
    """A user out of both is told about the wall with the longer horizon."""
    db.upsert_subscriber(3, "u", "en")
    consume_quota(db, 3, M)
    st = get_quota_state(db, 3)
    assert st.monthly_exhausted and st.daily_exhausted
    assert st.limit_kind == "monthly"


# ── the roll ─────────────────────────────────────────────────────────────────────────────────


def test_the_daily_meter_rolls_at_the_utc_day_boundary(db: Database) -> None:
    """No cron, no timer: a stale `alerts_day` reads as zero, which IS the roll."""
    db.upsert_subscriber(4, "u", "en")
    consume_quota(db, 4, D)
    assert get_quota_state(db, 4).daily_exhausted is True

    _set_day(db, 4, _yesterday(), D)
    st = get_quota_state(db, 4)
    assert st.day_used == 0, "yesterday's count is not today's"
    assert st.daily_exhausted is False
    assert st.used == D, "the MONTHLY counter does not roll with the day"


def test_consuming_after_a_roll_restamps_today(db: Database) -> None:
    db.upsert_subscriber(5, "u", "en")
    _set_day(db, 5, _yesterday(), 42)
    st = consume_quota(db, 5, 1)
    assert st.day_used == 1, "the stale day was discarded, not incremented"
    with db._cursor() as cur:
        cur.execute("SELECT alerts_day, alerts_day_count FROM subscribers WHERE chat_id = 5")
        row = cur.fetchone()
    assert row["alerts_day"] == _utc_day_key()
    assert row["alerts_day_count"] == 1


# ── the two episode keys ─────────────────────────────────────────────────────────────────────


def test_the_daily_notice_re_arms_next_day_while_the_monthly_one_does_not(db: Database) -> None:
    """The reason `quota_day_notice_day` exists at all.

    Reusing `quota_100_last_fired_at` for the daily wall would announce it at most ONCE EVER: the
    monthly stamp stays >= window_start for up to 30 days, so day 2's wall would be silent.
    """
    db.upsert_subscriber(6, "u", "en")
    consume_quota(db, 6, D)
    st = get_quota_state(db, 6)
    assert st.limit_kind == "daily"
    assert _notice_due(st) is True, "first daily wall announces"

    db.mark_quota_day_notice(6, _utc_day_key())
    assert _notice_due(get_quota_state(db, 6)) is False, "same day, already told"

    # Next UTC day: still walled (count forced), and the notice must re-arm.
    db.mark_quota_day_notice(6, _yesterday())
    assert _notice_due(get_quota_state(db, 6)) is True, "a new UTC day re-arms the daily notice"


def test_the_daily_lane_does_not_touch_the_monthly_stamp(db: Database) -> None:
    db.upsert_subscriber(7, "u", "en")
    consume_quota(db, 7, D)
    db.mark_quota_day_notice(7, _utc_day_key())
    with db._cursor() as cur:
        cur.execute(
            "SELECT quota_100_last_fired_at, quota_day_notice_day FROM subscribers WHERE chat_id = 7"
        )
        row = cur.fetchone()
    assert row["quota_100_last_fired_at"] is None, "the monthly episode key is untouched"
    assert row["quota_day_notice_day"] == _utc_day_key()


def test_evaluate_delivery_projects_the_limit_kind(db: Database) -> None:
    """CH2d single-derivation: the decision names the wall, so copy never re-decides it."""
    db.upsert_subscriber(8, "u", "en")
    consume_quota(db, 8, D)
    dec = evaluate_delivery(db, 8)
    assert dec.allowed is False
    assert dec.limit_kind == "daily"
    assert dec.limit_kind == dec.state.limit_kind, "projected, not independently re-derived"


# ── the ladder mirror: absent, stale, live — and it always SERVES ────────────────────────────


def test_an_absent_mirror_serves_the_pinned_fallbacks(db: Database) -> None:
    """A ladder we could not read is never a reason to refuse anyone."""
    lad = resolve_ladder(db)
    assert lad.source == "fallback"
    assert (lad.free_monthly, lad.free_daily) == (M, D)
    assert (lad.starter_price_usd, lad.starter_monthly_calls) == (
        STARTER_PRICE_USD, STARTER_MONTHLY_CALLS,
    )
    db.upsert_subscriber(9, "u", "en")
    st = get_quota_state(db, 9)
    assert st.exhausted is False, "the fallback SERVES"


def test_a_stale_mirror_serves_the_pinned_fallbacks(db: Database) -> None:
    stale = datetime.now(timezone.utc) - LADDER_STALE_AFTER - timedelta(hours=1)
    db.upsert_free_tier_ladder(999, 888, 1.23, 4567, stale.isoformat())
    lad = resolve_ladder(db)
    assert lad.source == "fallback", "older than LADDER_STALE_AFTER"
    assert (lad.free_monthly, lad.free_daily) == (M, D)


def test_a_fresh_mirror_overrides_the_pinned_fallbacks(db: Database) -> None:
    """The whole point: the constants are a floor, the mirror is the answer."""
    now = datetime.now(timezone.utc).isoformat()
    db.upsert_free_tier_ladder(555, 44, 19.99, 50_000, now)
    lad = resolve_ladder(db)
    assert lad.source == "mirror"
    assert (lad.free_monthly, lad.free_daily) == (555, 44)
    assert (lad.starter_price_usd, lad.starter_monthly_calls) == (19.99, 50_000)

    db.upsert_subscriber(10, "u", "en")
    st = get_quota_state(db, 10)
    assert st.total == 555 and st.day_total == 44
    assert st.starter_price_usd == 19.99 and st.starter_monthly_calls == 50_000


def test_the_mirror_row_is_a_singleton(db: Database) -> None:
    """`CHECK (id = 1)` — without it a retried write mints a second row and the read becomes
    ordering-dependent, which is a load-bearing property rented from SQLite."""
    now = datetime.now(timezone.utc).isoformat()
    db.upsert_free_tier_ladder(200, 100, 9.99, 10_000, now)
    db.upsert_free_tier_ladder(201, 101, 8.88, 11_000, now)
    with db._cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM free_tier_ladder")
        assert cur.fetchone()["n"] == 1
    assert resolve_ladder(db).free_monthly == 201, "the upsert REPLACED, it did not append"


# ── the contract the rest of the suite depends on ────────────────────────────────────────────


def test_quota_state_still_constructs_positionally() -> None:
    """The dataclass's own warning, pinned.

    Most of this suite builds QuotaState positionally. A new field without a default breaks every
    one of those at CONSTRUCTION — before any assertion runs — and the failure names the wrong
    thing entirely.
    """
    st = QuotaState(47, 100, None, 0.47)
    assert st.used == 47 and st.total == 100
    assert st.day_used == 0 and st.day_total == D
    assert st.quota_day_notice_day is None
    assert st.starter_price_usd == STARTER_PRICE_USD


def test_a_paid_subscriber_has_no_free_limit_kind(db: Database) -> None:
    """`limit_kind` describes the FREE lane's walls. The paid lane has its own." """
    st = QuotaState(0, M, None, 0.0, linked_tier="starter", day_used=D + 5)
    assert st.daily_exhausted is True, "the raw predicate still computes"
    assert st.limit_kind is None, "but it is not what walls a paying customer"


# ── the stamping that FOLLOWS the decision (found in review, not by the tests above) ─────────


def test_the_free_daily_wall_stamps_its_OWN_key_not_the_monthly_one(db: Database) -> None:
    """`refuse_and_notify` must stamp the episode key `_notice_due` will actually read.

    The three lanes wall on three clocks and therefore carry three stamps. Before this test the
    free DAILY wall fell through to the MONTHLY stamp — which the daily branch of `_notice_due`
    never reads — so the notice would have re-fired on every dispatch cycle for as long as the
    user stayed walled, and corrupted the monthly episode key on the way past.
    """
    import asyncio

    from algovault_bot.quota import refuse_and_notify

    db.upsert_subscriber(20, "u", "en")
    consume_quota(db, 20, D)
    dec = evaluate_delivery(db, 20)
    assert dec.limit_kind == "daily" and dec.notify is True

    sent: list[str] = []

    async def _send(text: str, markup=None) -> bool:
        sent.append(text)
        return True

    assert asyncio.run(refuse_and_notify(db, 20, "watch", send=_send, decision=dec)) is True
    assert len(sent) == 1

    with db._cursor() as cur:
        cur.execute(
            "SELECT quota_100_last_fired_at, quota_day_notice_day FROM subscribers WHERE chat_id = 20"
        )
        row = cur.fetchone()
    assert row["quota_day_notice_day"] == _utc_day_key(), "the DAILY key was stamped"
    assert row["quota_100_last_fired_at"] is None, "the MONTHLY key was left alone"

    # ...and the notice is now spent for today: a second cycle must NOT re-fire.
    assert evaluate_delivery(db, 20).notify is False


def test_the_free_monthly_wall_still_stamps_the_monthly_key(db: Database) -> None:
    """The regression guard for the branch above: monthly behaviour is byte-identical."""
    import asyncio

    from algovault_bot.quota import refuse_and_notify

    db.upsert_subscriber(21, "u", "en")
    consume_quota(db, 21, M)
    dec = evaluate_delivery(db, 21)
    assert dec.limit_kind == "monthly"

    async def _send(_text: str, _markup=None) -> bool:
        return True

    assert asyncio.run(refuse_and_notify(db, 21, "watch", send=_send, decision=dec)) is True
    with db._cursor() as cur:
        cur.execute("SELECT quota_100_last_fired_at FROM subscribers WHERE chat_id = 21")
        assert cur.fetchone()["quota_100_last_fired_at"] is not None


# ── the notice ledger records WHICH wall (the +30d impact wave depends on it) ────────────────


def _notices(db: Database) -> list[tuple[int, str | None]]:
    with db._cursor() as cur:
        cur.execute("SELECT chat_id, limit_kind FROM quota_notices_fired ORDER BY id")
        return [(r["chat_id"], r["limit_kind"]) for r in cur.fetchall()]


def test_the_notice_ledger_records_which_wall_fired(db: Database) -> None:
    """🛑 Without this, `GROWTH-TG-DAILY-CAP-IMPACT-W1` cannot answer its own question.

    `alerts_fired` is written ONLY on the delivered path, so once the daily cap ships it is
    censored at the cap by construction — the CH0/P5 query ("free chat-days above 100") can
    never return a new row whether the cap bound zero times or five hundred. That is a
    confident ZERO from an instrument structurally incapable of seeing the thing.

    `quota_notices_fired` is the one table the REFUSAL path writes, so it is the only durable
    per-episode record. It has to say WHICH wall, or the +30d re-measure reads the daily and
    monthly walls as one undifferentiated count.
    """
    import asyncio

    from algovault_bot.quota import refuse_and_notify

    async def _send(_t: str, _markup=None) -> bool:
        return True

    # daily wall
    db.upsert_subscriber(30, "u", "en")
    consume_quota(db, 30, D)
    asyncio.run(refuse_and_notify(db, 30, "watch", send=_send, decision=evaluate_delivery(db, 30)))

    # monthly wall
    db.upsert_subscriber(31, "u", "en")
    consume_quota(db, 31, M)
    asyncio.run(refuse_and_notify(db, 31, "watch", send=_send, decision=evaluate_delivery(db, 31)))

    assert _notices(db) == [(30, "daily"), (31, "monthly")]


def test_the_impact_query_can_separate_the_two_walls(db: Database) -> None:
    """The exact shape `GROWTH-TG-DAILY-CAP-IMPACT-W1` will run at +30 days."""
    import asyncio

    from algovault_bot.quota import refuse_and_notify

    async def _send(_t: str, _markup=None) -> bool:
        return True

    for i, units in ((40, D), (41, D), (42, M)):
        db.upsert_subscriber(i, "u", "en")
        consume_quota(db, i, units)
        asyncio.run(refuse_and_notify(db, i, "watch", send=_send, decision=evaluate_delivery(db, i)))

    with db._cursor() as cur:
        cur.execute(
            "SELECT limit_kind, COUNT(*) n, COUNT(DISTINCT chat_id) chats "
            "FROM quota_notices_fired GROUP BY limit_kind ORDER BY limit_kind"
        )
        rows = {r["limit_kind"]: (r["n"], r["chats"]) for r in cur.fetchall()}
    assert rows["daily"] == (2, 2), "two distinct subscribers hit the DAILY wall"
    assert rows["monthly"] == (1, 1)


def test_a_pre_migration_row_reads_NULL_not_a_backfilled_guess(db: Database) -> None:
    """An absent measurement is not a zero.

    Every row written before 2026-08-27 predates the daily cap, so it is monthly by
    construction — but backfilling them to 'monthly' would be inventing a measurement nobody
    took. The impact wave must be able to tell "we recorded monthly" from "we did not record".
    """
    db.upsert_subscriber(50, "u", "en")
    db.record_quota_notice_fired(50)  # the pre-migration call shape
    assert _notices(db) == [(50, None)]


# ── the digest surfaces WHICH wall, so the answer arrives daily instead of once ──────────────


def test_the_digest_breaks_notices_out_by_wall(db: Database) -> None:
    """GROWTH-TG-QUOTA-PARITY-W1 follow-up: replaces a calendar reminder with a daily signal.

    "Re-measure in ~30 days" is prose addressed to whoever happens to read it — the control this
    wave spent three chapters retiring. A daily wall firing is now visible on the day it fires.
    """
    import asyncio

    from algovault_bot.digest import compute_digest_metrics
    from algovault_bot.quota import refuse_and_notify

    async def _send(_t: str, _markup=None) -> bool:
        return True

    for cid, units in ((60, D), (61, D), (62, M)):
        db.upsert_subscriber(cid, "u", "en")
        consume_quota(db, cid, units)
        asyncio.run(
            refuse_and_notify(db, cid, "watch", send=_send, decision=evaluate_delivery(db, cid))
        )

    m = compute_digest_metrics(db)
    assert (m.quota_notices_daily_24h, m.quota_notices_monthly_24h) == (2, 1)
    assert m.quota_notices_unclassified_24h == 0
    # The total is DERIVED from the parts, so a breakdown can never disagree with its own total.
    assert m.quota_notices_24h == 3


def test_the_split_renders_on_the_digest_line(db: Database) -> None:
    import asyncio

    from algovault_bot.digest import render_digest
    from algovault_bot.quota import refuse_and_notify

    async def _send(_t: str, _markup=None) -> bool:
        return True

    db.upsert_subscriber(70, "u", "en")
    consume_quota(db, 70, D)
    asyncio.run(refuse_and_notify(db, 70, "watch", send=_send, decision=evaluate_delivery(db, 70)))

    body = render_digest(db)
    assert "🔒 Quota-exhausted notices: 1  (🗓 Monthly 0 · ⏰ Daily 1)" in body
    assert "Unclassified" not in body, "the unclassified part is hidden when zero"


def test_pre_migration_rows_are_shown_not_silently_dropped(db: Database) -> None:
    """A breakdown whose parts do not sum to its total reads as a bug in the digest.

    Rows written before the 2026-08-28 migration carry `limit_kind IS NULL`. They are honest
    missing provenance, not zero — so they are counted and labelled rather than discarded.
    """
    from algovault_bot.digest import compute_digest_metrics, render_digest

    db.upsert_subscriber(80, "u", "en")
    db.record_quota_notice_fired(80)  # the pre-migration call shape
    m = compute_digest_metrics(db)
    assert (m.quota_notices_monthly_24h, m.quota_notices_daily_24h) == (0, 0)
    assert m.quota_notices_unclassified_24h == 1
    assert m.quota_notices_24h == 1
    assert "❔ Unclassified 1" in render_digest(db)
