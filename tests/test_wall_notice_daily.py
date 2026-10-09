"""The free wall's ratified copy, pinned byte-for-byte — now against the composer.

LINEAGE. This file began as GROWTH-TG-QUOTA-PARITY-W1 CH3's guard on `paywall.format_paywall_body`
and its four levels. GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1 R2c retired that
renderer: three of its four levels were unreachable (nothing called the selector that could pick
`soft`/`hard`, and `build_refusal_text` never passed the referral arguments), and the two that
WERE live are now composed by `notices.compose_wall` so that one function owns the body and the
keyboard together.

WHY THIS FILE WAS REWRITTEN RATHER THAN DELETED, which is the load-bearing part: its `daily_block`
cases pin copy Mr.1 ratified on 2026-08-27, in three languages, with a docstring saying a change
here "must fail rather than ship quietly". Deleting the file with its retired subject would have
dropped that guard silently — the copy would have moved to a new renderer with nothing watching
it. So the pins move to where the strings now live, and the assertions that only described the
dead levels are gone with them.

WHAT IS PINNED:
1. §Copy B (the DAILY wall) — the 2026-08-27 ratification, re-ratified verbatim by this wave's
   dispatch, in en / id / zh-hans.
2. §Copy A (the MONTHLY wall) — this wave's ratification, in all three.
3. The single derivation: `state.limit_kind` picks the wall, so the wall a user is TOLD about is
   by construction the wall that stopped them. A daily-walled user is never told to wait 30 days.
4. The <= 300-char T2 voice rule, per language.
5. No `algovault.com` anywhere in a body — the class this wave retires.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from algovault_bot.db import Database
from algovault_bot.notices import compose_wall
from algovault_bot.quota import (
    FREE_TIER_DAILY_QUOTA,
    QuotaState,
    _fallback_ladder,
    build_refusal_text,
    consume_quota,
    get_quota_state,
)

_NOW = datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc)


def _daily_walled() -> QuotaState:
    return QuotaState(50, 200, _NOW - timedelta(days=5), 0.25, day_used=100, day_total=100)


def _monthly_walled() -> QuotaState:
    return QuotaState(200, 200, _NOW - timedelta(days=5), 1.0)


# ── 1. §Copy B — the DAILY wall, ratified 2026-08-27, re-ratified by this dispatch ───────────


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en", "Daily limit reached (100/100 alerts today). Resets 00:00 UTC. "
               "Tap a plan below to upgrade."),
        ("id", "Batas harian tercapai (100/100 alert hari ini). Direset pukul 00:00 UTC. "
               "Ketuk paket di bawah untuk upgrade."),
        ("zh-hans", "已达每日上限（今日 100/100 条提醒）。UTC 00:00 重置。点击下方套餐升级。"),
    ],
)
def test_daily_wall_renders_the_ratified_copy_verbatim(lang: str, expected: str) -> None:
    """A change here is a public-copy change needing fresh ratification — it must FAIL, not ship."""
    assert compose_wall(_daily_walled(), _fallback_ladder(), None, lang).text == expected


def test_the_daily_wall_names_the_CLOCK_not_a_horizon() -> None:
    """The daily cap is a calendar boundary, so it can be NAMED. A rolling window cannot.

    Telling a user walled for two hours to come back in 30 days is the defect
    PRICING-FOLLOWUPS-GENERATOR-W1 CH1 fixed on the API side; this keeps it fixed here.
    """
    body = compose_wall(_daily_walled(), _fallback_ladder(), None, "en").text
    assert "00:00 UTC" in body
    assert "30-day" not in body and "30 days" not in body


# ── 2. §Copy A — the MONTHLY wall, ratified by this wave ─────────────────────────────────────


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en", "You've used all 200/200 free alerts. Resets 03 Oct 2026. "
               "Tap a plan below to upgrade."),
        ("id", "Anda telah memakai semua 200/200 alert gratis. Direset 03 Oct 2026. "
               "Ketuk paket di bawah untuk upgrade."),
        ("zh-hans", "您已用完全部 200/200 次免费提醒。03 Oct 2026 重置。点击下方套餐升级。"),
    ],
)
def test_monthly_wall_renders_the_ratified_copy_verbatim(lang: str, expected: str) -> None:
    assert compose_wall(_monthly_walled(), _fallback_ladder(), None, lang).text == expected


def test_the_monthly_wall_states_a_real_date_and_falls_back_when_it_cannot() -> None:
    """`window_start` is nullable — a subscriber who never consumed, and what the 30-day roll
    writes back. The naive f-string would render "Resets None."; the two-branch fallback is the
    ratified sentence `paywall.py` shipped, carried into `quota.reset_sentence`."""
    no_window = QuotaState(200, 200, None, 1.0)
    body = compose_wall(no_window, _fallback_ladder(), None, "en").text
    assert "Resets when your 30-day window rolls." in body
    assert "None" not in body


# ── 3. the single derivation, end to end ────────────────────────────────────────────────────


def test_daily_wall_is_reached_from_the_single_derivation(tmp_path) -> None:
    """CH2d -> CH3 -> V2: `build_refusal_text` picks the wall from `QuotaState.limit_kind`.

    The copy layer never re-decides it. Two independent derivations of one classification drift
    to contradiction, and here the contradiction is telling a user to wait 30 days when what
    stopped them resets at midnight.
    """
    db = Database(str(tmp_path / "t.db"))
    # GROWTH-TG-FREE-ALLOWANCE-W1: at the pinned ladder the monthly wall wins every tie with the
    # daily one (both are 100). The daily wall binds for a chat whose monthly allowance is above
    # the daily cap, so this runs against a mirror that publishes such a ladder.
    db.upsert_free_tier_ladder(
        2 * FREE_TIER_DAILY_QUOTA, FREE_TIER_DAILY_QUOTA, None, None,
        datetime.now(timezone.utc).isoformat(),
    )
    db.upsert_subscriber(1, "u", "en")
    consume_quota(db, 1, FREE_TIER_DAILY_QUOTA)
    state = get_quota_state(db, 1)
    assert state.limit_kind == "daily"

    notice = build_refusal_text(db, 1, state)
    assert "Daily limit reached" in notice.text
    assert "00:00 UTC" in notice.text
    assert f"{FREE_TIER_DAILY_QUOTA}/{FREE_TIER_DAILY_QUOTA}" in notice.text, (
        "the DAILY numbers, not the monthly ones"
    )
    assert notice.campaign == "quota_exhausted_push"
    assert notice.markup is not None, "the wall carries the picker"


# ── 4 + 5. the voice rules, on every body this file can reach ───────────────────────────────


@pytest.mark.parametrize("lang", ["en", "id", "zh-hans"])
@pytest.mark.parametrize("state_name", ["daily", "monthly"])
def test_every_wall_body_obeys_the_voice_rules(lang: str, state_name: str) -> None:
    state = _daily_walled() if state_name == "daily" else _monthly_walled()
    body = compose_wall(state, _fallback_ladder(), None, lang).text
    assert len(body) <= 300, f"{state_name}/{lang} is {len(body)} chars"
    assert "algovault.com" not in body, "a money CTA is a BUTTON, never a URL in the body"
    assert "x402" not in body, "retired from every human surface"
