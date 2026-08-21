"""BOT-W2 C2 — /start auth_<key> deep-link flow tests.

Verifies the bot-side branch of the attribution loop:
1. ``/start auth_<api_key>`` calls ``validate_api_key`` (mocked here),
2. on valid: links chat → api_key/tier in subscribers, emits the right reply,
3. on invalid: leaves subscribers untouched + emits the invalid-key message,
4. re-linking with same tier vs upgraded tier produces the right reply,
5. db helpers (link_subscriber / unlink_subscriber / get_linked_state) are
   exercised end-to-end.

Live HTTP round-trip against /api/bot/validate-key is NOT exercised here —
that lives in the C2 verification gate (curl against the Hetzner deploy).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from algovault_bot.db import Database
from algovault_bot.handlers import handle_link, handle_start
from algovault_bot.link_validator import KeyCheck


TEST_KEY = "av_live_aaaaaaaaaaaaaaaaaaaaaaaa"


# ── db helpers ─────────────────────────────────────────────────


def test_link_subscriber_first_time(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(42, "alice", "en")
    prev_tier, is_new = tmp_db.link_subscriber(42, TEST_KEY, "starter")
    assert is_new is True
    assert prev_tier is None
    api_key, tier = tmp_db.get_linked_state(42)
    assert api_key == TEST_KEY
    assert tier == "starter"


def test_link_subscriber_re_link_same_tier(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(42, "alice", "en")
    tmp_db.link_subscriber(42, TEST_KEY, "starter")
    prev_tier, is_new = tmp_db.link_subscriber(42, TEST_KEY, "starter")
    assert is_new is False
    assert prev_tier == "starter"


def test_link_subscriber_upgrade(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(42, "alice", "en")
    tmp_db.link_subscriber(42, TEST_KEY, "starter")
    prev_tier, is_new = tmp_db.link_subscriber(42, "av_live_NEW", "pro")
    assert is_new is False
    assert prev_tier == "starter"
    api_key, tier = tmp_db.get_linked_state(42)
    assert api_key == "av_live_NEW"
    assert tier == "pro"


def test_unlink_subscriber(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(42, "alice", "en")
    tmp_db.link_subscriber(42, TEST_KEY, "starter")
    tmp_db.unlink_subscriber(42)
    api_key, tier = tmp_db.get_linked_state(42)
    assert api_key is None
    assert tier is None


def test_get_linked_state_unknown_chat(tmp_db: Database) -> None:
    api_key, tier = tmp_db.get_linked_state(9999)
    assert api_key is None
    assert tier is None


# ── handle_link replies ────────────────────────────────────────


@pytest.fixture()
def _valid_starter() -> KeyCheck:
    return KeyCheck(
        status="VALID", tier="starter", customer_id="cus_test123", reason="ok"
    )


@pytest.fixture()
def _valid_pro() -> KeyCheck:
    return KeyCheck(status="VALID", tier="pro", customer_id="cus_test123", reason="ok")


@pytest.fixture()
def _determined_invalid() -> KeyCheck:
    """OPS-BOT-LINKED-TIER-REFRESH-W1 CH1: the invalid-key message is now reachable ONLY
    from a DETERMINED negative. `validate_api_key` no longer has a None inhabitant, so a
    test that used to stub `None` must say WHICH of the six former meanings it means."""
    return KeyCheck(
        status="INVALID", tier=None, customer_id=None, reason="no_active_subscription"
    )


def test_handle_link_first_time_starter(tmp_db: Database, _valid_starter: KeyCheck) -> None:
    with patch("algovault_bot.handlers.validate_api_key", return_value=_valid_starter):
        reply = handle_link(tmp_db, 42, "alice", "en", TEST_KEY)
    assert "✅ Linked!" in reply
    assert "starter" in reply
    # PRICING-BOT-DELIVERY-METERING-W1 CH6a: the bot no longer states an allowance it was never
    # told. `_TIER_QUOTA` hard-typed one and had been wrong since the ladder moved.
    assert "draw down your plan allowance" in reply
    api_key, tier = tmp_db.get_linked_state(42)
    assert api_key == TEST_KEY
    assert tier == "starter"


def test_handle_link_invalid_key(tmp_db: Database, _determined_invalid: KeyCheck) -> None:
    with patch(
        "algovault_bot.handlers.validate_api_key", return_value=_determined_invalid
    ):
        reply = handle_link(tmp_db, 42, "alice", "en", TEST_KEY)
    assert reply.startswith("❌")
    assert "wasn't recognized" in reply
    api_key, tier = tmp_db.get_linked_state(42)
    assert api_key is None
    assert tier is None


def test_handle_link_re_link_same_tier(tmp_db: Database, _valid_starter: KeyCheck) -> None:
    with patch("algovault_bot.handlers.validate_api_key", return_value=_valid_starter):
        handle_link(tmp_db, 42, "alice", "en", TEST_KEY)
        reply = handle_link(tmp_db, 42, "alice", "en", TEST_KEY)
    assert "already linked" in reply.lower()
    assert "starter" in reply


def test_handle_link_tier_upgrade(
    tmp_db: Database, _valid_starter: KeyCheck, _valid_pro: KeyCheck
) -> None:
    with patch("algovault_bot.handlers.validate_api_key", return_value=_valid_starter):
        handle_link(tmp_db, 42, "alice", "en", TEST_KEY)
    with patch("algovault_bot.handlers.validate_api_key", return_value=_valid_pro):
        reply = handle_link(tmp_db, 42, "alice", "en", "av_live_NEW")
    assert "starter → pro" in reply
    # CH6a: the tier CHANGE is stated; the allowance is not, because the bot has no ladder to
    # state it from. 15,000 was the retired dict's figure and had been wrong for a long time.
    assert "draw down your plan allowance" in reply
    api_key, tier = tmp_db.get_linked_state(42)
    assert api_key == "av_live_NEW"
    assert tier == "pro"


def test_handle_link_creates_subscriber_implicitly(
    tmp_db: Database, _valid_starter: KeyCheck
) -> None:
    # New chat going straight to /start auth_<key> with no prior /start
    with patch("algovault_bot.handlers.validate_api_key", return_value=_valid_starter):
        handle_link(tmp_db, 999, "newuser", "en", TEST_KEY)
    assert tmp_db.get_subscriber(999) is not None


# ── plain /start preserves W1 behavior ─────────────────────────


def test_handle_start_no_param_unchanged(tmp_db: Database) -> None:
    reply = handle_start(tmp_db, 42, "alice", "en")
    assert "👋 Welcome to AlgoVault" in reply
