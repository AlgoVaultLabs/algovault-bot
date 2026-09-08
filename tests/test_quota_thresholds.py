"""End-to-end quota threshold + alert-format integration tests (C4)."""

from __future__ import annotations

from datetime import datetime, timezone

from algovault_bot.alert_engine import (
    WatchRow,
    format_regime_alert,
    format_trade_call_alert,
)
from algovault_bot.cta import trade_call_cta_text
from algovault_bot.notices import compose_caption_cta
from algovault_bot.quota import _fallback_ladder
from algovault_bot.quota import QuotaState


def _row(alert_type: str = "both") -> WatchRow:
    return WatchRow(
        chat_id=1, coin="BTC", timeframe="4h", exchange="BINANCE",
        alert_type=alert_type, regime_last_seen=None,
        last_verdict=None, last_verdict_streak=0,
    )


def _state(used: int) -> QuotaState:
    return QuotaState(used, 100, datetime.now(timezone.utc), used / 100)


# AC4.1
def test_alert_at_47_no_cta() -> None:
    quota = _state(47)
    cta = trade_call_cta_text(quota) or None
    msg = format_trade_call_alert(
        _row(), "BUY", 78, 84250.50, "TRENDING_UP", "NORMAL", "trend up", quota, cta=cta,
    )
    assert "📊 Quota: 47/100 free alerts used" in msg
    assert "utm_campaign=quota_75" not in msg
    assert "utm_campaign=quota_90" not in msg
    assert "utm_campaign=quota_100" not in msg


# AC4.2 — soft 75% nudge fires once per 24h per user.
def test_alert_at_80_quota_75_cta() -> None:
    quota = _state(80)  # last_75/last_90 default None → first fire
    # V2 CH1 R3 — the bucket is the decision; `notices.compose_caption_cta` is the body, and its
    # copy is asserted in `tests/test_notices.py`. What this AC pins is that the alert renders
    # the quota line AND carries whatever caption the bucket produced.
    assert trade_call_cta_text(quota) == "75"
    notice = compose_caption_cta("75", quota, _fallback_ladder(), None)
    msg = format_trade_call_alert(
        _row(), "BUY", 78, 84250.50, "TRENDING_UP", "NORMAL", None, quota, cta=notice.text,
    )
    assert "📊 Quota: 80/100 free alerts used" in msg
    assert "75% of your free alerts" in msg
    assert "algovault.com" not in msg, "the CTA is a BUTTON now, never a URL in the body"


# AC4.3 — urgent 90% nudge fires once per 24h per user.
def test_alert_at_95_quota_90_cta() -> None:
    quota = _state(95)
    assert trade_call_cta_text(quota) == "90"
    notice = compose_caption_cta("90", quota, _fallback_ladder(), None)
    msg = format_trade_call_alert(
        _row(), "SELL", 80, 84250.50, "TRENDING_DOWN", "ELEVATED", None, quota, cta=notice.text,
    )
    assert "📊 Quota: 95/100 free alerts used" in msg
    assert "Only 5 free alerts left" in msg
    assert "algovault.com" not in msg, "the CTA is a BUTTON now, never a URL in the body"


# AC4.4 — BOT-QUOTA-REFUSAL-SEAM-W1 retired `format_quota_exhausted_alert`: it was
# the walled-user body for the ONE lane that had one, which is how three lanes ended
# up with three behaviours. The body is now `quota.build_refusal_text`, shared by
# every push lane. The assertions below hold the same contract against the new body.
def test_walled_body_states_the_wall_and_carries_a_button_not_a_url(tmp_path) -> None:
    from algovault_bot.db import Database
    from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA, build_refusal_text, get_quota_state

    db = Database(str(tmp_path / "t.db"))
    db.upsert_subscriber(1, "u", "en")
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=?",
            (FREE_TIER_MONTHLY_QUOTA, datetime.now(timezone.utc).isoformat(), 1),
        )
    notice = build_refusal_text(db, 1, get_quota_state(db, 1))
    msg = notice.text
    # GROWTH-TG-QUOTA-PARITY-W1: rendered from the constant this test already imports. The old
    # literal duplicated a value the very next line derives.
    assert f"{FREE_TIER_MONTHLY_QUOTA}/{FREE_TIER_MONTHLY_QUOTA}" in msg
    assert "alerts" in msg, "the BOT's unit, not the API's 'calls'"
    # V2 CH1 R3 — the x402 clause is GONE from every human surface: a person in Telegram cannot
    # pay per call with it, so it was an API noun on a consumer body. The rail itself is
    # untouched and still named where it IS actionable (`alert_image.py`'s "X402 Plan" card
    # label, `quota.PAID_TIERS`); this asserts its ABSENCE here, which is the ratified change.
    assert "x402" not in msg
    # And the CTA is a BUTTON. The campaign rides `plan_picker_kb`, so the body carries no URL
    # at all — that is the whole wave, asserted at the surface that dispatched it.
    assert "algovault.com" not in msg
    assert notice.campaign == "quota_exhausted_push"
    assert notice.markup is not None
    assert "Resets" in msg and "30 days" not in msg, (
        "must name the real rolling-window date, not a calendar-month horizon"
    )


# AC4.5 — the regime alert renders WITHOUT a CTA, which is now the only reachable shape:
# V2 CH1 R3 deleted `regime_cta_text` and its always-False gate, and `alert_engine` passes
# `cta=None` unconditionally. The former "#1 includes a CTA" case is gone with the copy it
# pinned — it asserted a URL in a body no user could ever have received.
def test_regime_alert_no_cta_at_2() -> None:
    msg = format_regime_alert(_row(), "RANGING", "TRENDING_UP", 76, cta=None)
    assert "utm_campaign" not in msg
