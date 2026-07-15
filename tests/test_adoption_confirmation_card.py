"""TG-ADOPTION-CONFIRM-MSG-W1 — the one-tap adoption buttons (sw: 'set a standing scan',
wb: one-tap watch) must send the SAME persistent confirmation card as the typed /scanwatch
and /watch paths (tap==type parity), not just a transient callback toast.

The async callbacks are thin shells (closures inside register_handlers, not importable); the
card logic lives in these pure builders — this is the unit-testable seam. Wiring (callback →
reply_text) is verified live at deploy."""
from __future__ import annotations

from algovault_bot import adoption, handlers, messages


def test_scanwatch_button_card_is_byte_identical_to_typed_renderer() -> None:
    card = handlers.adoption_scanwatch_confirmation_card()
    assert card == messages.format_subscription_confirmation(
        "scanwatch",
        top_n=adoption.SCANWATCH_DEFAULT_TOP_N,
        tf=adoption.SCANWATCH_DEFAULT_TF,
        exchange=adoption.SCANWATCH_DEFAULT_EXCHANGE,
        cadence=adoption.SCANWATCH_DEFAULT_CADENCE,
    )
    # content anchors: the durable message states the real re-check cadence (the TF)
    assert f"top {adoption.SCANWATCH_DEFAULT_TOP_N}" in card
    assert f"re-check every {adoption.SCANWATCH_DEFAULT_TF}" in card
    assert "NEW BUY/SELL" in card and "/unscanwatch" in card


def test_watch_button_card_is_byte_identical_to_typed_renderer() -> None:
    data = adoption.build_watch_callback("ETH", "4h", "BYBIT", adoption.SOURCE_DIGEST)
    card = handlers.adoption_watch_confirmation_card(data)
    assert card == messages.format_subscription_confirmation(
        "watch", coin="ETH", tf="4h", exchange="BYBIT", mode=handlers.DEFAULT_ALERT_TYPE
    )
    assert "ETH · 4h · BYBIT" in card and "/unwatch" in card


def test_watch_button_card_none_on_malformed_payload() -> None:
    # symmetric with handle_adoption_watch_tap → no card sent when nothing was created
    assert handlers.adoption_watch_confirmation_card("wb:bogus") is None


def test_scanwatch_toast_states_the_timeframe_not_the_vestigial_cadence(tmp_db) -> None:
    # regression: post-Approach-B the toast said "every 1h" (the vestigial cadence column)
    # while dispatch is on the 15m TF — the toast must state the real re-check interval.
    data = adoption.build_scanwatch_callback(adoption.SOURCE_SCAN_SHOWCASE)
    toast = handlers.handle_adoption_scanwatch_tap(tmp_db, 501, "u", "en", data)
    assert toast is not None
    assert f"every {adoption.SCANWATCH_DEFAULT_TF}" in toast
    assert "every 1h" not in toast
