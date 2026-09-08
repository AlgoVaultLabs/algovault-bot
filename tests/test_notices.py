"""Every money notice this bot can compose — copy, campaign, keyboard, voice.

GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1 R6.

The wave's whole claim is that a money CTA is a BUTTON. This file is where that is asserted for
every composer, in every language it serves, rather than at nine separate call sites that used
to each assemble their own body and their own URL.

The single most important assertion here is the dullest one: NO `algovault.com` IN ANY BODY. It
is what the operator's failed tap on 2026-09-07 reduces to, and it is checked on every composer
× every language rather than sampled.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from algovault_bot.notices import (
    MAX_BODY_CHARS,
    WALL_FOLLOWUP_3D,
    WALL_FOLLOWUP_7D,
    MoneyNotice,
    compose_caption_cta,
    compose_followup,
    compose_plan_wall,
    compose_pull_refusal,
    compose_wall,
)
from algovault_bot.quota import QuotaState, _fallback_ladder

LANGS = ["en", "id", "zh-hans"]
_NOW = datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc)
LAD = _fallback_ladder()


def _monthly() -> QuotaState:
    return QuotaState(200, 200, _NOW - timedelta(days=5), 1.0)


def _daily() -> QuotaState:
    return QuotaState(50, 200, _NOW - timedelta(days=5), 0.25, day_used=100, day_total=100)


def _every_trilingual_notice():
    """(label, notice) for every composer that takes a `lang`, in all three languages."""
    for lang in LANGS:
        yield f"wall/monthly/{lang}", compose_wall(_monthly(), LAD, None, lang)
        yield f"wall/daily/{lang}", compose_wall(_daily(), LAD, None, lang)
        for source in ("scan", "regime", "call", "funding"):
            yield (
                f"pull/{source}/{lang}",
                compose_pull_refusal(source, _monthly(), LAD, None, lang),
            )
        yield (
            f"followup/3d/{lang}",
            compose_followup(WALL_FOLLOWUP_3D, _monthly(), LAD, None, lang,
                             n_watches=3, days_since_wall=5),
        )
        yield (
            f"followup/7d/{lang}",
            compose_followup(WALL_FOLLOWUP_7D, _monthly(), LAD, None, lang,
                             n_watches=1, days_since_wall=9),
        )


ALL_TRILINGUAL = list(_every_trilingual_notice())


# ── THE wave assertion ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("label", "notice"), ALL_TRILINGUAL, ids=[a for a, _ in ALL_TRILINGUAL])
def test_no_body_carries_a_url(label: str, notice: MoneyNotice) -> None:
    """A scheme-less domain is what Telegram auto-links as `http://` and what failed to open.

    Checked on the DOMAIN and not on the scheme, deliberately: `https://…` in a body would be a
    working link but still the wrong shape — the CTA is the picker, and a link in prose is a
    second, un-attributed way to buy.
    """
    assert "algovault.com" not in notice.text, notice.text


@pytest.mark.parametrize(("label", "notice"), ALL_TRILINGUAL, ids=[a for a, _ in ALL_TRILINGUAL])
def test_every_notice_carries_the_picker(label: str, notice: MoneyNotice) -> None:
    assert notice.markup is not None
    urls = [b.url for row in notice.markup.inline_keyboard for b in row if b.url]
    assert urls, "the picker must render buttons"
    for u in urls:
        assert u.startswith("https://"), u
        assert f"utm_campaign={notice.campaign}" in u, u


@pytest.mark.parametrize(("label", "notice"), ALL_TRILINGUAL, ids=[a for a, _ in ALL_TRILINGUAL])
def test_every_body_obeys_the_voice_rules(label: str, notice: MoneyNotice) -> None:
    assert len(notice.text) <= MAX_BODY_CHARS, f"{label}: {len(notice.text)} chars"
    assert "x402" not in notice.text, "retired from every human surface"
    for sentence in re.split(r"(?<=[.!?])\s+", notice.text.strip()):
        words = [w for w in sentence.split() if w]
        assert len(words) <= 20, f"{label}: {len(words)} words — {sentence}"


# ── the horizon is the one thing the copy must never get wrong ───────────────────────────────


def test_the_daily_wall_names_the_clock_and_the_monthly_wall_names_the_date() -> None:
    """Two walls, two clocks, one derivation (`state.limit_kind`)."""
    assert "00:00 UTC" in compose_wall(_daily(), LAD, None, "en").text
    monthly = compose_wall(_monthly(), LAD, None, "en").text
    assert "03 Oct 2026" in monthly and "00:00 UTC" not in monthly


def test_a_pull_refusal_states_the_wall_that_actually_stopped_the_user() -> None:
    """All four sites used to interpolate the MONTHLY total while firing on monthly OR daily, so
    a user walled until midnight was shown the 30-day cap beside a midnight clock."""
    daily = compose_pull_refusal("scan", _daily(), LAD, None, "en").text
    assert "100/100 alerts today" in daily and "00:00 UTC" in daily
    assert "200" not in daily, "the monthly cap must not appear beside a midnight clock"


def test_a_window_that_never_started_renders_the_ratified_fallback_not_None() -> None:
    body = compose_wall(QuotaState(200, 200, None, 1.0), LAD, None, "en").text
    assert "Resets when your 30-day window rolls." in body
    assert "None" not in body


# ── the paid wall reads the SERVER's next rung ───────────────────────────────────────────────


def _paid(next_plan: dict | None) -> QuotaState:
    import json

    s = QuotaState(0, 200, _NOW, 0.0, linked_tier="starter")
    s.plan_used, s.plan_total, s.plan_limit_kind = 9_876, 10_000, "monthly"
    s.plan_period_start = (_NOW - timedelta(days=3)).isoformat()
    s.plan_next_json = json.dumps(next_plan) if next_plan else None
    return s


def test_the_paid_wall_renders_the_servers_label_and_allowance() -> None:
    n = compose_plan_wall(
        _paid({"id": "pro", "label": "Pro", "monthly_calls": 100_000}),
        LAD, None, "en", next_plan={"id": "pro", "label": "Pro", "monthly_calls": 100_000},
        top_of_ladder="TOP",
    )
    assert "Tap Pro below — 100,000 alerts/mo." in n.text
    assert "calls/mo" not in n.text, "the bot's unit is alerts (METERING-DIVERGENCE Rule 1)"


def test_a_next_rung_without_a_label_falls_back_to_its_id_not_a_KeyError() -> None:
    """`nxt['label']` would raise on a live serving path; the shipped accessor is `.get`."""
    n = compose_plan_wall(
        _paid({"id": "pro", "monthly_calls": 100_000}), LAD, None, "en",
        next_plan={"id": "pro", "monthly_calls": 100_000}, top_of_ladder="TOP",
    )
    assert "Tap Pro below" in n.text, "the id is title-cased, never rendered bare lowercase"


def test_no_next_rung_means_the_ratified_sentence_and_NO_keyboard() -> None:
    n = compose_plan_wall(_paid(None), LAD, None, "en", next_plan=None, top_of_ladder="TOP")
    assert n.text.endswith("TOP")
    assert n.markup is None, "a button that leads nowhere is worse than no button"


# ── the captions ────────────────────────────────────────────────────────────────────────────


def test_the_captions_attach_the_picker_at_75_and_90_only() -> None:
    for threshold, needle in (("75", "75% of your free alerts"), ("90", "free alerts left")):
        n = compose_caption_cta(threshold, QuotaState(150, 200, _NOW, 0.75), LAD, None)
        assert needle in n.text
        assert n.campaign == f"quota_{threshold}"
        assert n.markup is not None


# ── the follow-ups ──────────────────────────────────────────────────────────────────────────


def test_the_d3_followup_states_the_users_OWN_elapsed_days_not_a_literal_3() -> None:
    """The first live cohort was aged 1.8–6.8 days; a hard-typed "3 days" is false for most."""
    for days in (3, 5, 6):
        n = compose_followup(WALL_FOLLOWUP_3D, _monthly(), LAD, None, "en",
                             n_watches=2, days_since_wall=days)
        assert f"paused for {days} days" in n.text


def test_the_d3_referral_clause_matches_what_the_code_actually_does() -> None:
    """The referee gets bonus alerts on JOINING; the referrer earns a commission IF THEY
    SUBSCRIBE. An earlier draft said "you both get bonus alerts", which is false for the
    referrer — `db.grant_referral_bonus` has exactly one caller, the referee's join."""
    text = compose_followup(WALL_FOLLOWUP_3D, _monthly(), LAD, None, "en",
                            n_watches=1, days_since_wall=4).text
    assert "friends get bonus alerts" in text
    assert "you earn commission if they subscribe" in text
    assert "you both get" not in text


def test_the_d7_followup_names_a_date_and_never_a_day_count() -> None:
    """`{days_left}` was dropped: at 1 it renders "1 days" and at 0 it names a future date and a
    zero in the same sentence. The date is the fact that matters and has no wrong render."""
    text = compose_followup(WALL_FOLLOWUP_7D, _monthly(), LAD, None, "en",
                            n_watches=4, days_since_wall=8).text
    assert "reset on 03 Oct 2026" in text
    assert "days until" not in text


@pytest.mark.parametrize("lang", LANGS)
def test_the_watch_count_is_translated_not_left_in_english(lang: str) -> None:
    """An English noun inside a translated sentence is the same class of defect as a hand-typed
    figure: correct-looking, and wrong for most readers."""
    text = compose_followup(WALL_FOLLOWUP_7D, _monthly(), LAD, None, lang,
                            n_watches=3, days_since_wall=9).text
    if lang == "en":
        assert "3 watches" in text
    else:
        assert "watches" not in text and "watch " not in text


def test_one_watch_is_singular_in_english() -> None:
    text = compose_followup(WALL_FOLLOWUP_7D, _monthly(), LAD, None, "en",
                            n_watches=1, days_since_wall=9).text
    assert "1 watch waiting" in text and "1 watches" not in text
