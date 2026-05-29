"""TG-BROADCAST-STACK-W1 CH5 (2026-05-28): X-follow verification tests.

Verifies:
1. is_pending_x_screenshot state predicate
2. compute_screenshot_path deterministic filename shape
3. screenshot_age_hours via filesystem mtime + missing-file handling
4. format_operator_review_caption + format_queue_alert_body content shape
5. End-to-end state transitions through Database:
   - pending_x → screenshot path set → verified + Pro grant inserted
   - pending_x → reset by reject → not_started
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from algovault_bot.screenshots import (
    QUEUE_REVIEW_SLA_HOURS,
    compute_screenshot_path,
    format_operator_review_caption,
    format_queue_alert_body,
    is_pending_x_screenshot,
    screenshot_age_hours,
)
from algovault_bot.unlock import (
    METHOD_X_FOLLOW,
    STATE_PENDING_X,
    STATE_VERIFIED,
    compute_grant_expiry,
)


FIXED_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


# ── is_pending_x_screenshot ─────────────────────────────────────────────


def test_is_pending_x_screenshot_true_only_for_pending_x_state():
    assert is_pending_x_screenshot("pending_x_screenshot") is True
    assert is_pending_x_screenshot("pending_npm_call") is False
    assert is_pending_x_screenshot("verified") is False
    assert is_pending_x_screenshot("not_started") is False
    assert is_pending_x_screenshot(None) is False


# ── compute_screenshot_path ──────────────────────────────────────────────


def test_compute_screenshot_path_deterministic_format(tmp_path):
    chat_id = 1234567
    now = FIXED_NOW
    path = compute_screenshot_path(chat_id, now=now, base_dir=tmp_path)
    assert path.parent == tmp_path
    assert path.name == "1234567-20260528T120000Z.jpg"


def test_compute_screenshot_path_distinguishes_per_chat(tmp_path):
    p1 = compute_screenshot_path(1, now=FIXED_NOW, base_dir=tmp_path)
    p2 = compute_screenshot_path(2, now=FIXED_NOW, base_dir=tmp_path)
    assert p1.name != p2.name


# ── screenshot_age_hours ─────────────────────────────────────────────────


def test_screenshot_age_returns_none_when_missing(tmp_path):
    missing = tmp_path / "does-not-exist.jpg"
    assert screenshot_age_hours(missing, now=FIXED_NOW) is None


def test_screenshot_age_zero_for_freshly_written(tmp_path):
    p = tmp_path / "fresh.jpg"
    p.write_bytes(b"x")
    # Use the file's actual mtime as `now` (avoids races on slow CI).
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    age = screenshot_age_hours(p, now=mtime)
    assert age == pytest.approx(0.0, abs=0.01)


def test_screenshot_age_grows_over_5h(tmp_path):
    """Backdate file mtime via os.utime → screenshot_age_hours reports >= 5h."""
    p = tmp_path / "old.jpg"
    p.write_bytes(b"x")
    five_hours_ago = time.time() - 5 * 3600
    os.utime(p, (five_hours_ago, five_hours_ago))
    age = screenshot_age_hours(p)
    assert age is not None
    assert age >= QUEUE_REVIEW_SLA_HOURS


# ── Caption + queue alert body shape ─────────────────────────────────────


def test_format_operator_review_caption_with_username():
    caption = format_operator_review_caption(9999, "alice", "en")
    assert "@alice" in caption
    assert "chat_id=9999" in caption
    assert "Approve" in caption
    assert "Reject" in caption


def test_format_operator_review_caption_no_username():
    caption = format_operator_review_caption(9999, None, None)
    assert "chat_id=9999" in caption
    assert "@" not in caption  # no rogue at-sign when username absent


def test_format_queue_alert_body_includes_pending_count_and_age():
    body = format_queue_alert_body(pending_count=3, oldest_age_hours=8.5)
    assert "Pending screenshots: 3" in body
    assert "8.5h" in body
    # Recommended-wave template form (per CLAUDE.md template-form rule).
    assert "W{NEXT}" in body
    assert "TG_UNLOCK_SCREENSHOT_QUEUE_PENDING" in body


# ── End-to-end DB state transitions ──────────────────────────────────────


@pytest.fixture
def x_subscriber(tmp_db):
    chat_id = 9201
    tmp_db.upsert_subscriber(chat_id, "x_test", "en")
    tmp_db.set_unlock_pending(chat_id, STATE_PENDING_X, METHOD_X_FOLLOW)
    return chat_id


def test_e2e_approve_transitions_to_verified_and_grants_pro(tmp_db, x_subscriber, tmp_path):
    """Simulate the [Approve] callback: state → verified + Pro grant inserted."""
    # Screenshot already uploaded (set the path).
    screenshot = tmp_path / "9201-test.jpg"
    screenshot.write_bytes(b"img")
    tmp_db.set_unlock_screenshot_path(x_subscriber, str(screenshot))

    # Approve.
    now = FIXED_NOW
    expires = compute_grant_expiry(now)
    tmp_db.set_unlock_verified(x_subscriber, now.isoformat())
    tmp_db.insert_or_replace_pro_grant(
        x_subscriber, expires.isoformat(), METHOD_X_FOLLOW
    )

    status, _, screenshot_path, _ = tmp_db.get_unlock_state(x_subscriber)
    assert status == STATE_VERIFIED
    assert screenshot_path == str(screenshot)

    grant = tmp_db.get_pro_grant(x_subscriber)
    assert grant is not None
    assert grant["method"] == METHOD_X_FOLLOW
    # expires_at parses back to a 30-day future datetime.
    expires_parsed = datetime.fromisoformat(str(grant["expires_at"]))
    if expires_parsed.tzinfo is None:
        expires_parsed = expires_parsed.replace(tzinfo=timezone.utc)
    assert expires_parsed > now


def test_e2e_reject_resets_to_not_started_no_grant(tmp_db, x_subscriber, tmp_path):
    """Simulate the [Reject] callback: state cleared; no Pro grant inserted."""
    screenshot = tmp_path / "9201-reject.jpg"
    screenshot.write_bytes(b"img")
    tmp_db.set_unlock_screenshot_path(x_subscriber, str(screenshot))

    tmp_db.reset_unlock_state(x_subscriber)

    status, method, screenshot_path, _ = tmp_db.get_unlock_state(x_subscriber)
    assert status is None
    assert method is None
    assert screenshot_path is None
    # No Pro grant should have been issued.
    assert tmp_db.get_pro_grant(x_subscriber) is None
