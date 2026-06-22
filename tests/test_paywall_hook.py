"""TG-BROADCAST-STACK-W1 CH3 (2026-05-28): paywall-at-quota hook tests.

Verifies:
1. Extraction of tier_warning from MCP response shape (presence + missing).
2. should_fire_paywall_dm logic — fires when warning present + not throttled.
3. mark_fired records timestamp; subsequent same-month check throttles.
4. format_paywall_body renders T1-voice ≤300 chars across en/id/zh-hans.
5. All 3 levels (soft/hard/block) handled correctly.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from algovault_bot.paywall import (
    extract_tier_warning,
    format_paywall_body,
    has_fired_this_month,
    mark_fired,
    should_fire_paywall_dm,
)


FIXED_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded_chat(tmp_db):
    """Seed a single subscriber row in the temp DB used across tests."""
    chat_id = 9001
    tmp_db.upsert_subscriber(chat_id, "test_user", "en")
    return chat_id


# ── extract_tier_warning ────────────────────────────────────────────────


def test_extract_tier_warning_returns_warning_block():
    response = {
        "verdict": "BUY",
        "confidence": 80,
        "_algovault": {
            "tier_warning": {
                "level": "soft",
                "current_usage": 75,
                "monthly_limit": 100,
                "tier": "free",
                "suggested_upgrade_url": "https://example.com",
            }
        },
    }
    warning = extract_tier_warning(response)
    assert warning is not None
    assert warning["level"] == "soft"


def test_extract_tier_warning_returns_none_when_absent():
    response = {"verdict": "BUY", "confidence": 80}
    assert extract_tier_warning(response) is None


def test_extract_tier_warning_returns_none_on_invalid_level():
    response = {"_algovault": {"tier_warning": {"level": "INVALID"}}}
    assert extract_tier_warning(response) is None


def test_extract_tier_warning_handles_non_dict_input():
    assert extract_tier_warning(None) is None
    assert extract_tier_warning([]) is None  # type: ignore[arg-type]
    assert extract_tier_warning("string") is None  # type: ignore[arg-type]


# ── format_paywall_body ─────────────────────────────────────────────────


def test_format_paywall_body_soft_en_under_300_chars():
    body = format_paywall_body("soft", 75, 100, "https://example.com/upgrade", "en")
    assert len(body) <= 300
    assert "/unlock_premium_alerts" in body
    assert "75" in body and "100" in body


def test_format_paywall_body_hard_en_under_300_chars():
    body = format_paywall_body("hard", 90, 100, "https://example.com/upgrade", "en")
    assert len(body) <= 300
    assert "/unlock_premium_alerts" in body


def test_format_paywall_body_block_en_under_300_chars():
    body = format_paywall_body("block", 100, 100, "https://example.com/upgrade", "en")
    assert len(body) <= 300
    assert "/unlock_premium_alerts" in body


def test_format_paywall_body_trilingual_id():
    body = format_paywall_body("soft", 75, 100, "https://example.com", "id")
    assert "Upgrade ke Pro" in body
    assert "/unlock_premium_alerts" in body
    assert len(body) <= 300


def test_format_paywall_body_trilingual_zh_hans():
    body = format_paywall_body("soft", 75, 100, "https://example.com", "zh-hans")
    assert "升级到 Pro" in body
    assert "/unlock_premium_alerts" in body
    assert len(body) <= 300


def test_format_paywall_body_unknown_lang_falls_back_to_en():
    body = format_paywall_body("soft", 75, 100, "https://example.com", "fr")
    assert "Upgrade to Pro" in body


def test_format_paywall_body_invalid_level_raises():
    with pytest.raises(ValueError):
        format_paywall_body("INVALID", 50, 100, "https://example.com", "en")


# ── REFERRAL-INPRODUCT-NUDGE-W1 / C2 — referral at the wall (block) ──────

_REF_LINK = "https://algovault.com/join?ref=ABCD1234"
_UPGRADE = "https://api.algovault.com/signup?plan=starter&upgrade_from=tg_quota"


def test_block_referral_en_prominent_upgrade_retained():
    body = format_paywall_body("block", 100, 100, _UPGRADE, "en", _REF_LINK, 500)
    assert "refer a friend" in body.lower()          # referral leads (prominent)
    assert "500 bonus calls" in body                  # SoT number (passed, not hardcoded)
    assert _REF_LINK in body                          # the user's own give-get link
    assert "upgrade" in body.lower() and _UPGRADE in body  # upgrade retained
    assert "/unlock_premium_alerts" in body           # existing free path kept (no removal)
    assert len(body) <= 300


def test_block_referral_trilingual_id_zh():
    body_id = format_paywall_body("block", 100, 100, _UPGRADE, "id", _REF_LINK, 500)
    assert "ajak teman" in body_id and "500 panggilan bonus" in body_id and _REF_LINK in body_id
    assert len(body_id) <= 300
    body_zh = format_paywall_body("block", 100, 100, _UPGRADE, "zh-hans", _REF_LINK, 500)
    assert "邀请好友" in body_zh and "500 次奖励调用" in body_zh and _REF_LINK in body_zh
    assert len(body_zh) <= 300


def test_block_failsoft_to_existing_copy_without_referral_args():
    # No referral_link / bonus_calls (engine fetch failed) → the existing block copy.
    body = format_paywall_body("block", 100, 100, _UPGRADE, "en")
    assert "refer a friend" not in body.lower()
    assert "/unlock_premium_alerts" in body            # existing copy intact
    assert len(body) <= 300


def test_soft_and_hard_ignore_referral_args():
    # Only the WALL (block) gets the referral arm — pre-wall warnings unchanged.
    for level in ("soft", "hard"):
        body = format_paywall_body(level, 80, 100, _UPGRADE, "en", _REF_LINK, 500)
        assert _REF_LINK not in body
        assert "refer a friend" not in body.lower()


def test_block_referral_no_outcome_leak():
    body = format_paywall_body("block", 100, 100, _UPGRADE, "en", _REF_LINK, 500)
    low = body.lower()
    assert "outcome_return_pct" not in low and "outcome_price" not in low and "profit" not in low


# ── has_fired_this_month + mark_fired ───────────────────────────────────


def test_has_fired_returns_false_on_fresh_subscriber(tmp_db, seeded_chat):
    assert not has_fired_this_month(tmp_db.path, seeded_chat, "soft", now=FIXED_NOW)
    assert not has_fired_this_month(tmp_db.path, seeded_chat, "hard", now=FIXED_NOW)
    assert not has_fired_this_month(tmp_db.path, seeded_chat, "block", now=FIXED_NOW)


def test_mark_fired_then_throttles_same_month(tmp_db, seeded_chat):
    mark_fired(tmp_db.path, seeded_chat, "soft", now=FIXED_NOW)
    assert has_fired_this_month(tmp_db.path, seeded_chat, "soft", now=FIXED_NOW)


def test_mark_fired_does_not_throttle_other_levels(tmp_db, seeded_chat):
    mark_fired(tmp_db.path, seeded_chat, "soft", now=FIXED_NOW)
    assert not has_fired_this_month(tmp_db.path, seeded_chat, "hard", now=FIXED_NOW)
    assert not has_fired_this_month(tmp_db.path, seeded_chat, "block", now=FIXED_NOW)


def test_throttle_resets_on_new_calendar_month(tmp_db, seeded_chat):
    """Fire in May; check in June → should be unblocked."""
    mark_fired(tmp_db.path, seeded_chat, "soft", now=FIXED_NOW)
    # Bump to next month
    next_month = FIXED_NOW.replace(month=6, day=1)
    assert not has_fired_this_month(tmp_db.path, seeded_chat, "soft", now=next_month)


# ── should_fire_paywall_dm orchestration ────────────────────────────────


def test_should_fire_returns_true_on_fresh_warning(tmp_db, seeded_chat):
    warning = {"level": "soft", "current_usage": 75, "monthly_limit": 100}
    fire, level = should_fire_paywall_dm(tmp_db.path, seeded_chat, warning, now=FIXED_NOW)
    assert fire is True
    assert level == "soft"


def test_should_fire_returns_false_after_mark(tmp_db, seeded_chat):
    warning = {"level": "hard", "current_usage": 90, "monthly_limit": 100}
    mark_fired(tmp_db.path, seeded_chat, "hard", now=FIXED_NOW)
    fire, level = should_fire_paywall_dm(tmp_db.path, seeded_chat, warning, now=FIXED_NOW)
    assert fire is False
    assert level == "hard"


def test_should_fire_returns_false_on_invalid_level(tmp_db, seeded_chat):
    warning = {"level": "INVALID"}
    fire, level = should_fire_paywall_dm(tmp_db.path, seeded_chat, warning, now=FIXED_NOW)
    assert fire is False
    assert level is None


# ── End-to-end: 3 thresholds fire correctly ─────────────────────────────


def test_e2e_3_thresholds_fire_idempotently(tmp_db, seeded_chat):
    """Synthetic 3-threshold cascade across the month — each fires once."""
    for level in ("soft", "hard", "block"):
        warning = {"level": level, "current_usage": 0, "monthly_limit": 100}
        # First call → fire
        fire, _ = should_fire_paywall_dm(tmp_db.path, seeded_chat, warning, now=FIXED_NOW)
        assert fire is True, f"first {level} call should fire"
        mark_fired(tmp_db.path, seeded_chat, level, now=FIXED_NOW)
        # Second call same level → throttled
        fire2, _ = should_fire_paywall_dm(tmp_db.path, seeded_chat, warning, now=FIXED_NOW)
        assert fire2 is False, f"second {level} call should be throttled"
