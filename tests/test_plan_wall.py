"""PRICING-BOT-DELIVERY-METERING-W1 CH5 — the paid-lane wall.

Two assertions here carry the chapter:
  * the FREE lane is byte-identical (a wave that walls paying users must not move the free meter);
  * a STALE or NEVER-OBSERVED mirror SERVES (never wall a paying customer on a measurement we
    could not take).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from algovault_bot.db import Database
from algovault_bot.quota import (
    FREE_TIER_MONTHLY_QUOTA,
    PLAN_MIRROR_STALE_AFTER,
    PlanState,
    build_plan_refusal_text,
    count_walled_now,
    evaluate_delivery,
    get_quota_state,
    refuse_and_notify,
)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def _mirror(allowed: int = 1, **over) -> dict:
    base = {
        "used": 5000, "total": 10000, "allowed": bool(allowed), "limit": None,
        "period_start": (NOW() - timedelta(days=3)).isoformat(),
        "daily_day": NOW().strftime("%Y-%m-%d"),
        "next_plan": {"id": "pro", "label": "Pro", "monthly_calls": 100000,
                      "price_usd": 49, "signup_url": "https://api.algovault.com/signup?plan=pro"},
    }
    base.update(over)
    return base


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(str(tmp_path / "t.db"))
    d.upsert_subscriber(1, "free", "en")
    d.upsert_subscriber(2, "paid", "en")
    d.link_subscriber(2, "av_live_k", "starter")
    return d


# ── the three states ────────────────────────────────────────────────────────


def test_paid_fresh_and_allowed_serves(db: Database) -> None:
    db.update_plan_mirror(2, _mirror(allowed=1), source="debit")
    d = evaluate_delivery(db, 2)
    assert d.state.plan_state is PlanState.ALLOW
    assert d.allowed is True


def test_paid_fresh_and_refused_walls_and_notifies(db: Database) -> None:
    db.update_plan_mirror(2, _mirror(allowed=0, limit="monthly", used=10000), source="debit")
    d = evaluate_delivery(db, 2)
    assert d.state.plan_state is PlanState.REFUSE
    assert d.allowed is False
    assert d.notify is True


def test_paid_STALE_mirror_SERVES(db: Database) -> None:
    """The load-bearing fail-open. A network blip must never wall a paying customer."""
    db.update_plan_mirror(2, _mirror(allowed=0), source="debit")
    stale = (NOW() - PLAN_MIRROR_STALE_AFTER - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    with db._cursor() as cur:
        cur.execute("UPDATE subscribers SET plan_state_as_of=? WHERE chat_id=2", (stale,))
    d = evaluate_delivery(db, 2)
    assert d.state.plan_state is PlanState.INDETERMINATE
    assert d.allowed is True, "a stale mirror must SERVE, never wall"


def test_paid_NEVER_observed_mirror_SERVES(db: Database) -> None:
    d = evaluate_delivery(db, 2)
    assert get_quota_state(db, 2).plan_state_as_of is None
    assert d.state.plan_state is PlanState.INDETERMINATE
    assert d.allowed is True


def test_uncapped_tier_never_walls(db: Database) -> None:
    """`total: null` = no ceiling. Reading it as 0 would wall an enterprise subscriber instantly."""
    db.update_plan_mirror(2, _mirror(allowed=1, total=None, used=None), source="poll")
    st = get_quota_state(db, 2)
    assert st.plan_total is None
    assert st.exhausted is False
    assert st.remaining == 10**9


def test_remaining_projects_the_plan_headroom(db: Database) -> None:
    db.update_plan_mirror(2, _mirror(used=9_990, total=10_000), source="debit")
    assert get_quota_state(db, 2).remaining == 10


# ── the FREE lane must not move ─────────────────────────────────────────────


def test_free_lane_is_byte_identical(db: Database) -> None:
    st = get_quota_state(db, 1)
    assert st.is_paid is False
    assert st.exhausted is False
    assert st.remaining == FREE_TIER_MONTHLY_QUOTA
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=1",
            (FREE_TIER_MONTHLY_QUOTA, NOW().isoformat()),
        )
    st = get_quota_state(db, 1)
    assert st.exhausted is True, "the free wall still fires at exactly the cap"
    assert st.remaining == 0
    assert evaluate_delivery(db, 1).notify is True


def test_a_plan_mirror_on_a_FREE_row_changes_nothing(db: Database) -> None:
    """Defence in depth: mirror columns exist on every row; only PAID reads them."""
    db.update_plan_mirror(1, _mirror(allowed=0, limit="monthly"), source="debit")
    assert evaluate_delivery(db, 1).allowed is True


# ── episode keys: monthly vs daily ──────────────────────────────────────────


def test_monthly_episode_fires_once_per_period(db: Database) -> None:
    db.update_plan_mirror(2, _mirror(allowed=0, limit="monthly"), source="debit")
    assert evaluate_delivery(db, 2).notify is True
    db.mark_quota_cta_fired(2, "100", NOW().isoformat())
    assert evaluate_delivery(db, 2).notify is False, "one notice per monthly episode"


def test_a_new_period_re_arms_the_monthly_notice(db: Database) -> None:
    db.mark_quota_cta_fired(2, "100", (NOW() - timedelta(days=40)).isoformat())
    db.update_plan_mirror(2, _mirror(allowed=0, limit="monthly"), source="debit")
    assert evaluate_delivery(db, 2).notify is True


def test_daily_episode_fires_once_per_UTC_day_and_RE_ARMS(db: Database) -> None:
    """The daily cap re-arms every UTC day. Reusing the monthly stamp would send ONE notice ever —
    the subscriber would hit the wall again tomorrow and hear nothing."""
    today = NOW().strftime("%Y-%m-%d")
    db.update_plan_mirror(2, _mirror(allowed=0, limit="daily", daily_day=today), source="debit")
    assert evaluate_delivery(db, 2).notify is True

    db.mark_plan_wall_notice_day(2, today)
    assert evaluate_delivery(db, 2).notify is False, "already told today"

    tomorrow = (NOW() + timedelta(days=1)).strftime("%Y-%m-%d")
    db.update_plan_mirror(2, _mirror(allowed=0, limit="daily", daily_day=tomorrow), source="debit")
    assert evaluate_delivery(db, 2).notify is True, "the daily wall must re-arm the next day"


def test_the_daily_wall_stamps_its_OWN_key(db: Database) -> None:
    today = NOW().strftime("%Y-%m-%d")
    db.update_plan_mirror(2, _mirror(allowed=0, limit="daily", daily_day=today), source="debit")
    sent: list[str] = []

    async def _send(text: str, markup=None) -> bool:
        sent.append(text)
        return True

    assert asyncio.run(refuse_and_notify(db, 2, "watch", send=_send)) is True
    row = db.get_subscriber(2)
    assert row["plan_wall_notice_day"] == today
    assert row["quota_100_last_fired_at"] is None, "the daily wall must not burn the monthly stamp"


# ── the copy fabricates nothing ─────────────────────────────────────────────


def test_plan_refusal_text_contains_no_figure_absent_from_its_inputs(db: Database) -> None:
    """AC5 — asserted MECHANICALLY, not by eye. `messages._TIER_QUOTA` hard-coded the ladder and
    was wrong for every linked subscriber from the day it moved; this is the guard against a
    repeat."""
    m = _mirror(allowed=0, limit="monthly", used=9_876, total=10_000)
    db.update_plan_mirror(2, m, source="debit")
    st = get_quota_state(db, 2)
    text = build_plan_refusal_text(db, 2, st)

    allowed_numbers = set()
    for v in (st.plan_used, st.plan_total):
        if v is not None:
            allowed_numbers |= {str(v), f"{v:,}"}
    nxt = json.loads(st.plan_next_json or "{}")
    for v in nxt.values():
        if isinstance(v, int):
            allowed_numbers |= {str(v), f"{v:,}"}
        elif isinstance(v, str):
            allowed_numbers |= set(re.findall(r"\d[\d,]*", v))
    # The reset date is derived from plan_period_start, which is an input.
    allowed_numbers |= set(re.findall(r"\d[\d,]*", (st.plan_period_start or "")))
    allowed_numbers |= set(re.findall(r"\d[\d,]*", (datetime.fromisoformat(st.plan_period_start) + timedelta(days=30)).strftime("%d %b %Y")))

    for token in re.findall(r"\d[\d,]*", text):
        assert token in allowed_numbers, f"{token!r} appears in the copy but in no input"


def test_enterprise_next_plan_null_renders_without_a_fabricated_figure(db: Database) -> None:
    db.update_plan_mirror(2, _mirror(allowed=0, limit="monthly", next_plan=None), source="debit")
    text = build_plan_refusal_text(db, 2, get_quota_state(db, 2))
    assert "top self-serve plan" in text
    assert "signup?plan=" not in text, "no next rung must be invented"


def test_plan_refusal_text_is_trilingual(db: Database) -> None:
    db.update_plan_mirror(2, _mirror(allowed=0, limit="monthly"), source="debit")
    for lang, needle in (("id", "Kuota paket"), ("zh-hans", "套餐额度"), ("fr", "plan allowance")):
        with db._cursor() as cur:
            cur.execute("UPDATE subscribers SET lang_code=? WHERE chat_id=2", (lang,))
        assert needle in build_plan_refusal_text(db, 2, get_quota_state(db, 2))


def test_the_paid_wall_uses_the_PLAN_copy_not_the_free_copy(db: Database) -> None:
    db.update_plan_mirror(2, _mirror(allowed=0, limit="monthly"), source="debit")
    sent: list[str] = []

    async def _send(text: str, markup=None) -> bool:
        sent.append(text)
        return True

    asyncio.run(refuse_and_notify(db, 2, "watch", send=_send))
    assert sent and "plan allowance used" in sent[0]
    assert "free alerts" not in sent[0], "a paying subscriber must not be told about the free tier"


# ── the paid dimension on the operator count ────────────────────────────────


def test_count_walled_now_separates_paid_from_free(db: Database) -> None:
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=1",
            (FREE_TIER_MONTHLY_QUOTA, NOW().isoformat()),
        )
    db.update_plan_mirror(2, _mirror(allowed=0, limit="monthly"), source="debit")
    walled, silent, walled_paid = count_walled_now(db)
    assert walled == 2
    assert walled_paid == 1, "a walled PAYING subscriber is a distinct operator signal"
