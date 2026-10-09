"""GROWTH-TG-FREE-ALLOWANCE-W1 R2.5 — the daily clause renders only where it can bind.

Ruling R-2: the daily cap is unchanged (= the API's per-day cap), but against a 100-alert month it
cannot be the wall that stops anyone on the ladder, so stating it reads as a second limit that does
not exist. ONE predicate (`messages.daily_cap_is_material`) decides it for every `{daily_total}`
copy site: `welcome_message` (two sentences), `help_message` (one), `notices.compose_downgrade`
(EN / ID / ZH — pinned verbatim in tests/test_link_lifecycle.py).

Both branches are pinned:
  • NOT material → the §Copy A / B sentences, spelled out in full below.
  • material     → BYTE-IDENTICAL to the pre-wave output. The goldens under
                   tests/fixtures/allowance_copy/ were rendered from the UNMODIFIED origin/main
                   code (`227c4cb`) for the 200 / 100 ladder, not re-typed from the new template —
                   a golden produced by the code under test could only ever agree with it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from algovault_bot.messages import daily_cap_is_material, help_message, welcome_message
from algovault_bot.quota import (
    FREE_TIER_DAILY_QUOTA,
    FREE_TIER_MONTHLY_QUOTA,
    STARTER_MONTHLY_CALLS,
    STARTER_PRICE_6MONTH_USD,
    STARTER_PRICE_USD,
)

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "allowance_copy"


def _welcome(monthly: int, daily: int) -> str:
    return welcome_message(
        monthly, daily, STARTER_PRICE_USD, STARTER_MONTHLY_CALLS, STARTER_PRICE_6MONTH_USD
    )


@pytest.mark.parametrize(
    "monthly,daily,material",
    [(200, 100, True), (100, 100, False), (100, 200, False), (101, 100, True), (1, 1, False)],
)
def test_the_daily_cap_is_material_only_below_the_monthly_allowance(
    monthly: int, daily: int, material: bool
) -> None:
    assert daily_cap_is_material(monthly, daily) is material


def test_the_pinned_ladder_is_not_material() -> None:
    """The ruling, as a property of the shipped constants: 100 a month beside 100 a day."""
    assert daily_cap_is_material(FREE_TIER_MONTHLY_QUOTA, FREE_TIER_DAILY_QUOTA) is False


# ── NOT material: §Copy A / B, verbatim ─────────────────────────────────────────────────────────


def test_welcome_states_the_month_only_when_the_day_cannot_bind() -> None:
    body = _welcome(100, 100)
    assert (
        "You get 100 free alerts a month. Each alert I send uses one. Silent HOLDs are always free."
        in body
    )
    assert (
        "Free: 100 alerts/month. Want more? Starter is $9.99/mo or $39.90/6mo for "
        "10,000 API calls/mo." in body
    )
    assert "a day" not in body and "/day" not in body


def test_help_states_the_month_only_when_the_day_cannot_bind() -> None:
    body = help_message(100, 100)
    assert (
        "Free tier: 100 alerts a month. Regime and BUY/SELL alerts each use one. "
        "Silent HOLDs are free." in body
    )
    assert "a day" not in body


# ── material: byte-identical to the pre-wave output ───────────────────────────────────────────


def test_welcome_is_byte_identical_to_the_pre_wave_text_when_the_day_binds() -> None:
    expected = (GOLDEN / "welcome_200_100.golden.txt").read_text(encoding="utf-8")
    assert _welcome(200, 100) == expected
    assert "You get 200 free alerts a month, up to 100 a day." in expected  # the golden is real


def test_help_is_byte_identical_to_the_pre_wave_text_when_the_day_binds() -> None:
    expected = (GOLDEN / "help_200_100.golden.txt").read_text(encoding="utf-8")
    assert help_message(200, 100) == expected
    assert "Free tier: 200 alerts a month, up to 100 a day." in expected


def test_only_the_daily_clause_differs_between_the_branches() -> None:
    """Removing exactly the clause, nothing else: the non-material text is the material text with
    the two (welcome) / one (help) daily clauses deleted and the figures moved to the ladder."""
    w_material = _welcome(200, 100).replace("200", "N")
    w_plain = _welcome(100, 100).replace("100 free alerts", "N free alerts").replace(
        "100 alerts/month", "N alerts/month"
    )
    assert w_material.replace(", up to 100 a day", "").replace(", 100/day", "") == w_plain
    h_material = help_message(200, 100).replace("200", "N")
    h_plain = help_message(100, 100).replace("100 alerts a month.", "N alerts a month.").replace(
        "more than 100 alerts", "more than N alerts"
    )
    assert h_material.replace(", up to 100 a day", "") == h_plain
