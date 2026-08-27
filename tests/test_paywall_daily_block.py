"""GROWTH-TG-QUOTA-PARITY-W1 CH3 — the fourth paywall level, and the three that must not move.

Two jobs, and the first one is the more important:

1. **The existing `soft` / `hard` / `block` levels render BYTE-IDENTICALLY** to before this wave,
   in all three languages. CH3 re-pointed their `else 100` fallback at a constant and made their
   price + paid-rung figures derive from the ladder mirror; none of that may change a character of
   what ships. The expected strings below were captured from the pre-change renderer.
2. The NEW `daily_block` level renders the copy ratified by Mr.1 on 2026-08-27, verbatim.
"""
from __future__ import annotations

import pytest

from algovault_bot.paywall import format_paywall_body
from algovault_bot.quota import STARTER_MONTHLY_CALLS, STARTER_PRICE_USD

URL = "https://u.example"


# ── 1. the three incumbent levels are frozen ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en", "You've used 47/200 alerts. Upgrade to Starter ($9.99/mo, 10,000 API calls): "
               f"{URL}, OR earn 30 days free Pro via /unlock_premium_alerts."),
        ("id", "Anda telah memakai 47/200 alert. Upgrade ke Starter ($9,99/bln, 10.000 panggilan "
               f"API): {URL}, ATAU dapatkan 30 hari Pro gratis via /unlock_premium_alerts."),
        ("zh-hans", "您已使用 47/200 次提醒。升级到 Starter（$9.99/月、10,000 次 API 调用）："
                    f"{URL}，或通过 /unlock_premium_alerts 免费获取 30 天 Pro。"),
    ],
)
def test_soft_level_is_byte_identical(lang: str, expected: str) -> None:
    """The price and the paid rung now DERIVE — and must still render exactly as before.

    Note the locale split, which a single shared formatter would have quietly anglicised: `id`
    writes `$9,99` and `10.000` where `en` and `zh` write `$9.99` and `10,000`.
    """
    assert format_paywall_body("soft", 47, 200, URL, lang) == expected


@pytest.mark.parametrize("lang", ["en", "id", "zh-hans"])
@pytest.mark.parametrize("level", ["soft", "hard", "block"])
def test_the_incumbent_levels_ignore_the_new_ladder_arguments(level: str, lang: str) -> None:
    """Passing the ladder explicitly must equal not passing it, when it equals the pinned values.

    This is what proves the new parameters are a pass-through and not a behaviour change for any
    caller that has not been updated yet.
    """
    assert format_paywall_body(level, 47, 200, URL, lang) == format_paywall_body(
        level, 47, 200, URL, lang,
        starter_price_usd=STARTER_PRICE_USD,
        starter_monthly_calls=STARTER_MONTHLY_CALLS,
    )


def test_the_monthly_limit_fallback_is_the_constant_not_a_literal() -> None:
    """CH3b: `monthly_limit=None` used to fall back to a hand-typed 100."""
    from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA

    body = format_paywall_body("block", 5, None, URL, "en")
    assert f"/{FREE_TIER_MONTHLY_QUOTA}" in body


# ── 2. the new level ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en", f"Daily limit reached (100/100 alerts today). Resets 00:00 UTC. Upgrade: {URL}"),
        ("id", f"Batas harian tercapai (100/100 alert hari ini). Direset pukul 00:00 UTC. Upgrade: {URL}"),
        ("zh-hans", f"已达每日上限（今日 100/100 条提醒）。UTC 00:00 重置。升级：{URL}"),
    ],
)
def test_daily_block_renders_the_ratified_copy_verbatim(lang: str, expected: str) -> None:
    """Pins the approved wording. A change here is a public-copy change needing fresh
    ratification, so it must fail rather than ship quietly."""
    assert format_paywall_body("daily_block", 100, 100, URL, lang) == expected


@pytest.mark.parametrize("lang", ["en", "id", "zh-hans"])
def test_daily_block_names_the_CLOCK_never_a_horizon(lang: str) -> None:
    """The whole reason the daily cap is a calendar day.

    A rolling window resets on a date nobody can be told in advance; `00:00 UTC` can be stated.
    Telling a user walled for two hours to come back in 30 days is the exact defect
    PRICING-FOLLOWUPS-GENERATOR-W1 CH1 fixed on the API side.
    """
    body = format_paywall_body("daily_block", 100, 100, URL, lang)
    # Both token orders are correct: en/id say "00:00 UTC", the ratified zh string says
    # "UTC 00:00 重置" — which is the natural Chinese ordering, not a typo. Asserting the English
    # order in all three languages would demand a worse translation.
    assert ("00:00 UTC" in body) or ("UTC 00:00" in body)
    assert "30" not in body.replace(URL, ""), "no rolling-window horizon in the daily copy"


@pytest.mark.parametrize("lang", ["en", "id", "zh-hans"])
def test_every_level_stays_within_300_chars(lang: str) -> None:
    for level in ("soft", "hard", "block", "daily_block"):
        body = format_paywall_body(level, 47, 200, URL, lang)
        assert len(body) <= 300, f"{level}/{lang} is {len(body)} chars"


def test_daily_block_is_reached_from_the_single_derivation(tmp_path) -> None:
    """CH2d → CH3: `build_refusal_text` picks the level from `QuotaDecision.limit_kind`.

    The copy layer never re-decides which wall was hit, so the wall a user is TOLD about is by
    construction the wall that actually stopped them.
    """
    from algovault_bot.db import Database
    from algovault_bot.quota import FREE_TIER_DAILY_QUOTA, build_refusal_text, consume_quota, get_quota_state

    db = Database(str(tmp_path / "t.db"))
    db.upsert_subscriber(1, "u", "en")
    consume_quota(db, 1, FREE_TIER_DAILY_QUOTA)
    state = get_quota_state(db, 1)
    assert state.limit_kind == "daily"
    msg = build_refusal_text(db, 1, state)
    assert "Daily limit reached" in msg
    assert "00:00 UTC" in msg
    assert f"{FREE_TIER_DAILY_QUOTA}/{FREE_TIER_DAILY_QUOTA}" in msg, "the DAILY numbers, not the monthly ones"
