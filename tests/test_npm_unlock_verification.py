"""TG-BROADCAST-STACK-W1 CH6 (2026-05-28): npm-install verification tests.

Pure-function + state-machine coverage. The postgres-polling path is NOT
exercised here (would require live docker exec); end-to-end smoke happens
on Hetzner via synthetic --track-token call (audit doc verification step).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# Dynamically import the cron script under a clean module name (it lives in
# scripts/ outside the algovault_bot package).
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
CHECK_NPM_UNLOCKS_PATH = SCRIPTS_DIR / "check-npm-unlocks.py"
spec = importlib.util.spec_from_file_location(
    "check_npm_unlocks", str(CHECK_NPM_UNLOCKS_PATH)
)
assert spec is not None and spec.loader is not None
check_npm_unlocks = importlib.util.module_from_spec(spec)
sys.modules["check_npm_unlocks"] = check_npm_unlocks
spec.loader.exec_module(check_npm_unlocks)

from algovault_bot.unlock import (  # noqa: E402
    METHOD_NPM_INSTALL,
    STATE_PENDING_NPM,
    STATE_VERIFIED,
)


# ── extract_track_token ──────────────────────────────────────────────────


def test_extract_track_token_returns_token_from_valid_meta():
    meta = json.dumps({"track_token": "abc12345xyz", "tool_name": "get_trade_call"})
    assert check_npm_unlocks.extract_track_token(meta) == "abc12345xyz"


def test_extract_track_token_returns_none_on_empty_string():
    assert check_npm_unlocks.extract_track_token("") is None


def test_extract_track_token_returns_none_on_invalid_json():
    assert check_npm_unlocks.extract_track_token("{not json") is None


def test_extract_track_token_returns_none_on_missing_field():
    assert check_npm_unlocks.extract_track_token(json.dumps({"other": "x"})) is None


def test_extract_track_token_validates_length():
    too_short = json.dumps({"track_token": "abc"})
    too_long = json.dumps({"track_token": "x" * 100})
    assert check_npm_unlocks.extract_track_token(too_short) is None
    assert check_npm_unlocks.extract_track_token(too_long) is None


# ── find_pending_subscriber_for_token + DB state ─────────────────────────


@pytest.fixture
def npm_subscriber(tmp_db):
    """Seed a subscriber in pending_npm_call state with a known token."""
    chat_id = 9501
    token = "feedbeef" * 4  # 32-char hex
    tmp_db.upsert_subscriber(chat_id, "npm_test", "en")
    tmp_db.set_unlock_pending(
        chat_id, STATE_PENDING_NPM, METHOD_NPM_INSTALL, track_token=token
    )
    return chat_id, token


def test_find_pending_subscriber_for_token_matches(tmp_db, npm_subscriber):
    chat_id, token = npm_subscriber
    match = check_npm_unlocks.find_pending_subscriber_for_token(tmp_db.path, token)
    assert match is not None
    assert match[0] == chat_id


def test_find_pending_subscriber_for_token_no_match(tmp_db, npm_subscriber):
    match = check_npm_unlocks.find_pending_subscriber_for_token(
        tmp_db.path, "wrongtoken1"
    )
    assert match is None


def test_find_pending_subscriber_skips_already_verified(tmp_db, npm_subscriber):
    chat_id, token = npm_subscriber
    # Transition to verified
    tmp_db.set_unlock_verified(
        chat_id, datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    match = check_npm_unlocks.find_pending_subscriber_for_token(tmp_db.path, token)
    assert match is None


# ── grant_pro_to_subscriber ──────────────────────────────────────────────


def test_grant_pro_inserts_tg_pro_grants_row(tmp_db, npm_subscriber):
    chat_id, _ = npm_subscriber
    info = check_npm_unlocks.grant_pro_to_subscriber(tmp_db.path, chat_id)
    assert info["expires_iso"]
    grant = tmp_db.get_pro_grant(chat_id)
    assert grant is not None
    assert grant["method"] == "npm_install"


def test_grant_pro_sets_unlock_status_verified(tmp_db, npm_subscriber):
    chat_id, _ = npm_subscriber
    check_npm_unlocks.grant_pro_to_subscriber(tmp_db.path, chat_id)
    status, _, _, _ = tmp_db.get_unlock_state(chat_id)
    assert status == STATE_VERIFIED


def test_grant_pro_dry_run_no_db_mutation(tmp_db, npm_subscriber):
    chat_id, _ = npm_subscriber
    info = check_npm_unlocks.grant_pro_to_subscriber(tmp_db.path, chat_id, dry_run=True)
    assert info["expires_iso"]
    # No grant row, status unchanged.
    assert tmp_db.get_pro_grant(chat_id) is None
    status, _, _, _ = tmp_db.get_unlock_state(chat_id)
    assert status == STATE_PENDING_NPM


def test_grant_expiry_is_30_days_out(tmp_db, npm_subscriber):
    chat_id, _ = npm_subscriber
    info = check_npm_unlocks.grant_pro_to_subscriber(tmp_db.path, chat_id)
    now = datetime.now(timezone.utc)
    expires = datetime.fromisoformat(info["expires_iso"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delta_days = (expires - now).days
    # 30 days, allow ±1 for clock skew between calls.
    assert 29 <= delta_days <= 30


# ── expire_old_pending_npm ───────────────────────────────────────────────


def test_expire_old_pending_npm_skips_recent(tmp_db, npm_subscriber):
    """Fresh subscriber with linked_at=NULL → COALESCE falls back to
    last_seen_at which is fresh → NOT expired.
    """
    candidates = check_npm_unlocks.expire_old_pending_npm(tmp_db.path)
    assert candidates == []


def test_expire_old_pending_npm_catches_stale(tmp_db, npm_subscriber):
    """Backdate the subscriber's last_seen_at + linked_at + created_at to
    25h ago — expire_old_pending_npm picks it up + transitions to 'expired'.
    """
    chat_id, _ = npm_subscriber
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    import sqlite3
    conn = sqlite3.connect(tmp_db.path)
    conn.execute(
        "UPDATE subscribers SET last_seen_at = ?, created_at = ?, linked_at = ? "
        "WHERE chat_id = ?",
        (stale, stale, stale, chat_id),
    )
    conn.commit()
    conn.close()
    candidates = check_npm_unlocks.expire_old_pending_npm(tmp_db.path)
    assert any(c["chat_id"] == chat_id for c in candidates)
    # Verify state transitioned to 'expired'.
    status, _, _, _ = tmp_db.get_unlock_state(chat_id)
    assert status == "expired"


def test_expire_old_pending_npm_dry_run_no_mutation(tmp_db, npm_subscriber):
    chat_id, _ = npm_subscriber
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    import sqlite3
    conn = sqlite3.connect(tmp_db.path)
    conn.execute(
        "UPDATE subscribers SET last_seen_at = ?, created_at = ?, linked_at = ? "
        "WHERE chat_id = ?",
        (stale, stale, stale, chat_id),
    )
    conn.commit()
    conn.close()
    candidates = check_npm_unlocks.expire_old_pending_npm(tmp_db.path, dry_run=True)
    assert any(c["chat_id"] == chat_id for c in candidates)
    status, _, _, _ = tmp_db.get_unlock_state(chat_id)
    assert status == STATE_PENDING_NPM  # not changed
