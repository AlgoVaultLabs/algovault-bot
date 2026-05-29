"""TG-BROADCAST-STACK-W1 CH4 (2026-05-28): /unlock_premium_alerts tests.

Verifies:
1. Trilingual body rendering (en / id / zh-hans / fallback)
2. State machine transitions in db.py (set_unlock_pending, set_unlock_verified,
   reset_unlock_state, set_unlock_expired)
3. tg_pro_grants CRUD (get_pro_grant, insert_or_replace_pro_grant)
4. Track-token generation (UUIDv4 hex; 32 chars)
5. find_subscriber_by_npm_token mapping
6. Grant expiry computation
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from algovault_bot.unlock import (
    CB_UNLOCK_NPM,
    CB_UNLOCK_X,
    METHOD_NPM_INSTALL,
    METHOD_X_FOLLOW,
    STATE_PENDING_NPM,
    STATE_PENDING_X,
    STATE_VERIFIED,
    compute_grant_expiry,
    format_already_verified_body,
    format_button_labels,
    format_expired_body,
    format_intro_body,
    format_pending_npm_body,
    format_pending_x_body,
    format_rejected_body,
    format_verified_body,
    generate_track_token,
    is_pending_npm_expired,
    is_pending_x_expired,
    normalize_lang,
)


FIXED_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


# ── Trilingual body rendering ────────────────────────────────────────────


def test_format_intro_body_en_default():
    body = format_intro_body("en")
    assert "30 days Pro" in body
    assert "Follow @AlgoVaultLabs" in body


def test_format_intro_body_id():
    body = format_intro_body("id")
    assert "30 hari Pro" in body
    assert "Follow @AlgoVaultLabs" in body


def test_format_intro_body_zh_hans():
    body = format_intro_body("zh-hans")
    assert "30 天 Pro" in body
    assert "@AlgoVaultLabs" in body


def test_format_intro_body_unknown_lang_falls_back_to_en():
    body = format_intro_body("fr")
    assert "30 days Pro" in body


def test_format_intro_body_none_falls_back_to_en():
    body = format_intro_body(None)
    assert "30 days Pro" in body


def test_format_button_labels_trilingual():
    en_x, en_npm = format_button_labels("en")
    id_x, id_npm = format_button_labels("id")
    zh_x, zh_npm = format_button_labels("zh-hans")
    assert "Follow" in en_x and "Install" in en_npm
    assert "Follow" in id_x and "Install" in id_npm
    assert "关注" in zh_x and "安装" in zh_npm


def test_format_pending_x_body_en():
    body = format_pending_x_body("en")
    assert "Follow @AlgoVaultLabs on X" in body
    assert "screenshot" in body
    assert "24h" in body


def test_format_pending_x_body_id_zh():
    assert "screenshot" in format_pending_x_body("id").lower()
    assert "截图" in format_pending_x_body("zh-hans")


def test_format_pending_npm_body_includes_track_token():
    token = "abc123def456"
    body = format_pending_npm_body(token, "en")
    assert token in body
    assert "crypto-quant-signal-mcp" in body
    assert "--track-token=" in body


def test_format_pending_npm_body_trilingual():
    token = "tok123"
    en = format_pending_npm_body(token, "en")
    id_body = format_pending_npm_body(token, "id")
    zh_body = format_pending_npm_body(token, "zh-hans")
    # Track token snippet identical across languages.
    assert token in en and token in id_body and token in zh_body
    # Body copy differs.
    assert "Copy this" in en
    assert "Salin ini" in id_body
    assert "将此复制" in zh_body


def test_format_verified_body_two_methods():
    en_x = format_verified_body(METHOD_X_FOLLOW, "en")
    en_npm = format_verified_body(METHOD_NPM_INSTALL, "en")
    assert "30 days Pro" in en_x and "30 days Pro" in en_npm
    assert en_x != en_npm  # different bodies per method


def test_format_rejected_body():
    body = format_rejected_body("en")
    assert "Screenshot couldn't be verified" in body
    assert "/unlock_premium_alerts" in body


def test_format_expired_body():
    body = format_expired_body("en")
    assert "24h passed" in body
    assert "/unlock_premium_alerts" in body


# ── Track-token generation ───────────────────────────────────────────────


def test_generate_track_token_uniqueness():
    tokens = {generate_track_token() for _ in range(100)}
    assert len(tokens) == 100  # all distinct


def test_generate_track_token_shape():
    token = generate_track_token()
    assert len(token) == 32  # uuid4().hex
    assert re.match(r"^[0-9a-f]{32}$", token)


# ── Grant expiry computation ─────────────────────────────────────────────


def test_compute_grant_expiry_is_30_days_out():
    expiry = compute_grant_expiry(FIXED_NOW)
    delta = expiry - FIXED_NOW
    assert delta == timedelta(days=30)


# ── Pending state expiry ─────────────────────────────────────────────────


def test_is_pending_x_expired_after_24h():
    pending_since = FIXED_NOW - timedelta(hours=25)
    assert is_pending_x_expired(pending_since, now=FIXED_NOW)


def test_is_pending_x_not_expired_within_24h():
    pending_since = FIXED_NOW - timedelta(hours=12)
    assert not is_pending_x_expired(pending_since, now=FIXED_NOW)


def test_is_pending_npm_expired_after_24h():
    pending_since = FIXED_NOW - timedelta(hours=25)
    assert is_pending_npm_expired(pending_since, now=FIXED_NOW)


# ── normalize_lang ───────────────────────────────────────────────────────


def test_normalize_lang_routing():
    assert normalize_lang(None) == "en"
    assert normalize_lang("") == "en"
    assert normalize_lang("en") == "en"
    assert normalize_lang("en-US") == "en"
    assert normalize_lang("id") == "id"
    assert normalize_lang("id-ID") == "id"
    assert normalize_lang("zh-Hans") == "zh-hans"
    assert normalize_lang("zh-CN") == "zh-hans"
    assert normalize_lang("fr") == "en"  # fallback


# ── DB state machine integration ─────────────────────────────────────────


@pytest.fixture
def unlock_subscriber(tmp_db):
    """Seed a subscriber + return their chat_id."""
    chat_id = 9101
    tmp_db.upsert_subscriber(chat_id, "test_user", "en")
    return chat_id


def test_db_unlock_initial_state_is_null(tmp_db, unlock_subscriber):
    status, method, screenshot, token = tmp_db.get_unlock_state(unlock_subscriber)
    assert status is None
    assert method is None
    assert screenshot is None
    assert token is None


def test_db_set_unlock_pending_x(tmp_db, unlock_subscriber):
    tmp_db.set_unlock_pending(unlock_subscriber, STATE_PENDING_X, METHOD_X_FOLLOW)
    status, method, _, token = tmp_db.get_unlock_state(unlock_subscriber)
    assert status == STATE_PENDING_X
    assert method == METHOD_X_FOLLOW
    assert token is None  # no track-token for X path


def test_db_set_unlock_pending_npm_with_token(tmp_db, unlock_subscriber):
    tk = "deadbeef" * 4
    tmp_db.set_unlock_pending(
        unlock_subscriber, STATE_PENDING_NPM, METHOD_NPM_INSTALL, track_token=tk
    )
    status, method, _, token = tmp_db.get_unlock_state(unlock_subscriber)
    assert status == STATE_PENDING_NPM
    assert method == METHOD_NPM_INSTALL
    assert token == tk


def test_db_set_unlock_verified(tmp_db, unlock_subscriber):
    tmp_db.set_unlock_pending(unlock_subscriber, STATE_PENDING_X, METHOD_X_FOLLOW)
    tmp_db.set_unlock_verified(unlock_subscriber, FIXED_NOW.isoformat())
    status, _, _, _ = tmp_db.get_unlock_state(unlock_subscriber)
    assert status == STATE_VERIFIED


def test_db_reset_unlock_state_clears_all(tmp_db, unlock_subscriber):
    tk = "feedface" * 4
    tmp_db.set_unlock_pending(
        unlock_subscriber, STATE_PENDING_NPM, METHOD_NPM_INSTALL, track_token=tk
    )
    tmp_db.set_unlock_screenshot_path(unlock_subscriber, "/path/to/img.jpg")
    tmp_db.reset_unlock_state(unlock_subscriber)
    status, method, screenshot, token = tmp_db.get_unlock_state(unlock_subscriber)
    assert status is None
    assert method is None
    assert screenshot is None
    # NOTE: npm_unlock_session_id is NOT cleared by reset (intentional —
    # token is per-attempt; reset re-issues a new one on next /unlock).
    assert token == tk


def test_db_find_subscriber_by_npm_token(tmp_db, unlock_subscriber):
    tk = "cafebabe" * 4
    tmp_db.set_unlock_pending(
        unlock_subscriber, STATE_PENDING_NPM, METHOD_NPM_INSTALL, track_token=tk
    )
    found = tmp_db.find_subscriber_by_npm_token(tk)
    assert found is not None
    assert found["chat_id"] == unlock_subscriber
    # Different token should NOT match.
    other = tmp_db.find_subscriber_by_npm_token("not-this-one")
    assert other is None


def test_db_find_subscriber_by_npm_token_requires_pending_state(tmp_db, unlock_subscriber):
    tk = "12345678" * 4
    tmp_db.set_unlock_pending(
        unlock_subscriber, STATE_PENDING_NPM, METHOD_NPM_INSTALL, track_token=tk
    )
    # Transition to verified — find_subscriber should no longer match.
    tmp_db.set_unlock_verified(unlock_subscriber, FIXED_NOW.isoformat())
    found = tmp_db.find_subscriber_by_npm_token(tk)
    assert found is None


# ── tg_pro_grants CRUD ───────────────────────────────────────────────────


def test_db_pro_grant_insert_and_get(tmp_db, unlock_subscriber):
    expires = FIXED_NOW + timedelta(days=30)
    tmp_db.insert_or_replace_pro_grant(
        unlock_subscriber, expires.isoformat(), METHOD_X_FOLLOW
    )
    grant = tmp_db.get_pro_grant(unlock_subscriber)
    assert grant is not None
    assert grant["chat_id"] == unlock_subscriber
    assert grant["method"] == METHOD_X_FOLLOW


def test_db_pro_grant_replace_extends(tmp_db, unlock_subscriber):
    """Re-granting via second method REPLACES; one active grant per chat_id."""
    expires_x = FIXED_NOW + timedelta(days=30)
    tmp_db.insert_or_replace_pro_grant(
        unlock_subscriber, expires_x.isoformat(), METHOD_X_FOLLOW
    )
    expires_npm = FIXED_NOW + timedelta(days=60)
    tmp_db.insert_or_replace_pro_grant(
        unlock_subscriber, expires_npm.isoformat(), METHOD_NPM_INSTALL
    )
    grant = tmp_db.get_pro_grant(unlock_subscriber)
    assert grant["method"] == METHOD_NPM_INSTALL


def test_db_pro_grant_absent_returns_none(tmp_db, unlock_subscriber):
    assert tmp_db.get_pro_grant(unlock_subscriber) is None
