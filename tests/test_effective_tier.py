"""OPS-BOT-LINKED-TIER-REFRESH-W1 CH2 — tier is a PROJECTION, not a second copy.

`linked_tier` was written once at /link and never refreshed, while the server's current
tier arrived in every entitlement response and was discarded. `effective_tier` is the ONE
derivation every tier-labelled surface now projects from.

The live shape these tests encode was measured on 2026-08-21:

    chat 1061466212  linked_tier='starter'  plan_total=100000  server tier='pro'
    chat 1793689937  linked_tier='starter'  plan_state_as_of=NULL (unobserved)
    chat 8776880162  linked_tier='starter'  plan_total=10000   server tier='starter'
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from algovault_bot.alert_engine import WatchRow, format_trade_call_alert
from algovault_bot.db import Database
from algovault_bot.quota import (
    FREE_TIER_DAILY_QUOTA,
    FREE_TIER_MONTHLY_QUOTA,
    PAID_TIERS,
    PLAN_MIRROR_STALE_AFTER,
    QuotaState,
    _derive_tier,
    effective_tier,
    get_quota_state,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fresh() -> datetime:
    return _now() - timedelta(minutes=5)


def _stale() -> datetime:
    return _now() - PLAN_MIRROR_STALE_AFTER - timedelta(minutes=1)


# ── 2c — the three branches ────────────────────────────────────────────────────


def test_fresh_mirror_is_server_truth() -> None:
    assert _derive_tier("pro", _fresh(), "starter") == ("pro", "mirror")


def test_stale_mirror_falls_back_to_link_and_is_LABELLED() -> None:
    tier, source = _derive_tier("pro", _stale(), "starter")
    assert (tier, source) == ("starter", "link")


def test_unobserved_mirror_falls_back_to_link() -> None:
    assert _derive_tier(None, None, "starter") == ("starter", "link")


def test_no_link_at_all_is_unknown() -> None:
    assert _derive_tier(None, None, None) == (None, "unknown")


def test_a_stale_mirror_NEVER_renders_blank() -> None:
    """The failure this branch exists to prevent: a badge that goes empty when the
    drainer stalls. The floor of this whole change is exactly the prior behaviour."""
    for as_of in (None, _stale()):
        tier, _ = _derive_tier("pro", as_of, "starter")
        assert tier == "starter", "a stale mirror must fall back, not blank"


def test_mirror_without_an_as_of_is_not_trusted() -> None:
    """A tier with no freshness stamp is a fork, not a cache — it must not win."""
    assert _derive_tier("pro", None, "starter") == ("starter", "link")


def test_boundary_exactly_at_the_staleness_window_is_still_fresh() -> None:
    at_edge = _now() - PLAN_MIRROR_STALE_AFTER + timedelta(seconds=2)
    assert _derive_tier("pro", at_edge, "starter") == ("pro", "mirror")


def test_effective_tier_uses_the_SAME_window_as_the_wall() -> None:
    """One clock, not two. Two thresholds that must agree are two that will drift."""
    just_inside = _now() - PLAN_MIRROR_STALE_AFTER + timedelta(minutes=1)
    just_outside = _now() - PLAN_MIRROR_STALE_AFTER - timedelta(minutes=1)
    assert _derive_tier("pro", just_inside, "starter").source == "mirror"
    assert _derive_tier("pro", just_outside, "starter").source == "link"


# ── the row adapter ────────────────────────────────────────────────────────────


def test_row_adapter_matches_the_scalar_derivation(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "alice", "en")
    tmp_db.link_subscriber(1, "av_live_aaa", "starter")
    tmp_db.update_plan_mirror(1, {"tier": "pro", "used": 1, "total": 10}, source="debit")
    row = tmp_db.get_subscriber(1)
    assert effective_tier(row) == ("pro", "mirror")


def test_row_adapter_tolerates_a_row_predating_the_migration() -> None:
    class _Bare:
        def keys(self) -> list[str]:
            return ["linked_tier"]

        def __getitem__(self, k: str) -> str:
            return "starter"

    assert effective_tier(_Bare()) == ("starter", "link")


# ── 2f — chat 1061466212's EXACT shape renders Pro ─────────────────────────────


def test_the_1061466212_shape_renders_Pro_on_the_trade_call_card(
    tmp_db: Database,
) -> None:
    """The measured defect, end to end.

    linked_tier='starter' + a fresh mirror carrying tier='pro' and Pro's 100,000
    allowance. Before this chapter the card read "Starter plan" while the debit
    correctly charged Pro — two copies of one fact, and the label read the stale one.
    """
    tmp_db.upsert_subscriber(1061466212, "subject", "en")
    tmp_db.link_subscriber(1061466212, "av_live_subject", "starter")
    tmp_db.update_plan_mirror(
        1061466212,
        {"tier": "pro", "used": 5294, "total": 100000, "allowed": True},
        source="debit",
    )
    state = get_quota_state(tmp_db, 1061466212)

    assert state.linked_tier == "starter", "the stale copy is still on the row"
    assert state.plan_tier == "pro", "the mirror carries server truth"
    assert state.effective_tier == ("pro", "mirror")

    card = format_trade_call_alert(
        row=WatchRow(chat_id=1061466212, coin="BTC", timeframe="1h",
                     exchange="hyperliquid", alert_type="call",
                     regime_last_seen=None, last_verdict=None, last_verdict_streak=0),
        call="BUY", confidence=72, price=64000.0, regime="TRENDING",
        funding="0.01%", reasoning=None, quota=state,
    )
    assert "💎 Pro plan — 5,294/100,000 used" in card
    assert "Starter" not in card


def test_the_8776880162_shape_is_unchanged_by_this_wave(tmp_db: Database) -> None:
    """The control subject: genuinely starter, mirror agrees. Must not move."""
    tmp_db.upsert_subscriber(8776880162, "control", "en")
    tmp_db.link_subscriber(8776880162, "av_live_control", "starter")
    tmp_db.update_plan_mirror(
        8776880162,
        {"tier": "starter", "used": 575, "total": 10000, "allowed": True},
        source="debit",
    )
    state = get_quota_state(tmp_db, 8776880162)
    assert state.effective_tier == ("starter", "mirror")


def test_the_1793689937_shape_keeps_its_last_known_tier(tmp_db: Database) -> None:
    """Unobserved mirror. CH2's floor is today's behaviour — the dead-key leak is
    CH3's to close, not this chapter's, and CH2 must not pretend otherwise."""
    tmp_db.upsert_subscriber(1793689937, "dead", "en")
    tmp_db.link_subscriber(1793689937, "av_live_dead", "starter")
    state = get_quota_state(tmp_db, 1793689937)
    assert state.plan_tier is None and state.plan_state_as_of is None
    assert state.effective_tier == ("starter", "link")
    assert state.is_paid is True, "CH2 changes the LABEL, never the entitlement"


# ── 2e — x402 ──────────────────────────────────────────────────────────────────


def test_x402_renders_something_not_blank() -> None:
    """P7: `x402` is a PAID_TIERS member that `validateApiKey` cannot produce — its tier
    comes only from the price→tier registry (starter/pro/enterprise), and the entitlement
    body's `tier` is that same value, so CH2 does not make it reachable. Asserted anyway,
    because "unreachable" is a property of today's server and this is the cheap insurance
    against the first x402 subscriber getting a blank badge."""
    assert "x402" in PAID_TIERS
    tier, source = _derive_tier("x402", _fresh(), "starter")
    assert tier == "x402"
    assert tier.capitalize() == "X402"
    assert tier.capitalize().strip() != ""


def test_x402_renders_a_badge_on_the_card(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(4, "x402user", "en")
    tmp_db.link_subscriber(4, "av_live_x402", "x402")
    tmp_db.update_plan_mirror(4, {"tier": "x402", "used": 3, "total": None}, source="poll")
    state = get_quota_state(tmp_db, 4)
    card = format_trade_call_alert(
        row=WatchRow(chat_id=4, coin="ETH", timeframe="1h",
                     exchange="hyperliquid", alert_type="call",
                     regime_last_seen=None, last_verdict=None, last_verdict_streak=0),
        call="SELL", confidence=60, price=3000.0, regime="RANGING",
        funding="0.00%", reasoning=None, quota=state,
    )
    assert "💎 X402 plan" in card, "an x402 subscriber must never get a blank badge"


# ── 2f — free-tier behaviour must not move by one byte ─────────────────────────


def test_free_tier_row_is_untouched_by_the_mirror(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(9, "freeuser", "en")
    state = get_quota_state(tmp_db, 9)
    assert state.effective_tier == (None, "unknown")
    assert state.is_paid is False
    assert state.linked_tier is None
    assert state.plan_tier is None
    # GROWTH-TG-QUOTA-PARITY-W1: asserted against the CONSTANT, not a literal. These lines
    # hard-typed 100 and broke the moment the cap moved — which is the same defect, in a test,
    # that the wave is retiring in the shipped copy. A literal here would only reschedule it.
    assert state.total == FREE_TIER_MONTHLY_QUOTA
    assert state.used == 0
    assert state.remaining == FREE_TIER_MONTHLY_QUOTA
    assert state.exhausted is False
    assert state.day_total == FREE_TIER_DAILY_QUOTA
    assert state.day_used == 0


def test_free_tier_arithmetic_is_byte_identical_across_the_meter(
    tmp_db: Database,
) -> None:
    """A regression test that fails if ANY free arithmetic moves.

    The whole free ladder is walked: counter, remaining, pct and the exhaustion
    boundary. `effective_tier` returning "unknown" must keep every one of them on the
    free path, because a free subscriber has no mirror to project from and never will.
    """
    from algovault_bot.quota import consume_quota

    tmp_db.upsert_subscriber(10, "freeuser", "en")
    N = FREE_TIER_MONTHLY_QUOTA
    observed = []
    for _ in range(N):
        consume_quota(tmp_db, 10)
        s = get_quota_state(tmp_db, 10)
        observed.append((s.used, s.total, s.remaining, s.monthly_exhausted, s.is_paid))

    # GROWTH-TG-QUOTA-PARITY-W1: walked over the CONSTANT, so the ladder is re-walked at whatever
    # the cap becomes. The boundary column now reads `monthly_exhausted`, not `exhausted`: the
    # free lane gained a SECOND cap this wave, and `exhausted` is monthly-OR-daily. Asserting the
    # combined predicate here would be testing the daily meter inside a test named for the
    # monthly one — the daily cap has its own tests in test_quota_daily_cap.py.
    assert observed[0] == (1, N, N - 1, False, False)
    assert observed[N // 2 - 1] == (N // 2, N, N - N // 2, False, False)
    assert observed[N - 2] == (N - 1, N, 1, False, False)
    assert observed[N - 1] == (N, N, 0, True, False)
    assert all(t == N for (_u, t, _r, _e, _p) in observed)
    assert all(p is False for (*_rest, p) in observed)
    assert all(
        get_quota_state(tmp_db, 10).effective_tier == (None, "unknown") for _ in range(1)
    )


def test_free_tier_pct_used_ladder_is_unmoved(tmp_db: Database) -> None:
    from algovault_bot.quota import consume_quota

    tmp_db.upsert_subscriber(11, "freeuser", "en")
    # GROWTH-TG-QUOTA-PARITY-W1: the ASSERTION is that pct_used is used/total. Consuming a literal
    # 75 only equalled 0.75 while the cap happened to be 100; three-quarters of the CURRENT cap is
    # what the test was always about.
    for _ in range(FREE_TIER_MONTHLY_QUOTA * 3 // 4):
        consume_quota(tmp_db, 11)
    assert get_quota_state(tmp_db, 11).pct_used == pytest.approx(0.75)


def test_a_free_subscriber_never_gets_a_tier_badge(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(12, "freeuser", "en")
    state = get_quota_state(tmp_db, 12)
    card = format_trade_call_alert(
        row=WatchRow(chat_id=12, coin="BTC", timeframe="1h",
                     exchange="hyperliquid", alert_type="call",
                     regime_last_seen=None, last_verdict=None, last_verdict_streak=0),
        call="BUY", confidence=70, price=64000.0, regime="TRENDING",
        funding="0.01%", reasoning=None, quota=state,
    )
    assert "💎" not in card
    # GROWTH-TG-QUOTA-PARITY-W1: interpolated from the constant. The old literal was a FIXTURE
    # value, never the cap itself — the card has always rendered `{used}/{total}`.
    assert f"📊 Quota: 0/{FREE_TIER_MONTHLY_QUOTA} free alerts used" in card


# ── the mirror write carries tier from the SAME body ───────────────────────────


def test_update_plan_mirror_stores_tier_from_the_response(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(13, "a", "en")
    tmp_db.link_subscriber(13, "av_live_x", "starter")
    tmp_db.update_plan_mirror(
        13,
        {"tier": "pro", "used": 5294, "total": 100000, "allowed": True,
         "period_start": "2026-07-23T13:49:21.388Z", "daily_day": "2026-08-21"},
        source="debit",
    )
    row = tmp_db.get_subscriber(13)
    assert row["plan_tier"] == "pro"
    assert row["plan_state_source"] == "debit"
    assert row["plan_state_as_of"] is not None, "stamped by the EXISTING clock"


def test_a_response_without_tier_stores_NULL_and_falls_back(tmp_db: Database) -> None:
    """It must never overwrite a known tier with nothing, and NULL must read as
    unobserved rather than as a downgrade."""
    tmp_db.upsert_subscriber(14, "a", "en")
    tmp_db.link_subscriber(14, "av_live_x", "starter")
    tmp_db.update_plan_mirror(14, {"used": 1, "total": 10, "allowed": True}, source="poll")
    row = tmp_db.get_subscriber(14)
    assert row["plan_tier"] is None
    assert effective_tier(row) == ("starter", "link")


def test_quota_state_defaults_keep_positional_construction_working() -> None:
    """The suite constructs QuotaState POSITIONALLY; a non-defaulted field would break
    every one of those call sites at construction rather than at the assertion."""
    s = QuotaState(47, 100, None, 0.47)
    assert s.plan_tier is None
    assert s.effective_tier == (None, "unknown")
