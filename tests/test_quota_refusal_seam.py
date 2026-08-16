"""BOT-QUOTA-REFUSAL-SEAM-W1 — wire the structural gate into the test suite.

`scripts/check-quota-refusal-seam.py` is the real gate (verdict token, own
self-test, runnable standalone in CI or a hook). These tests make it run on every
`pytest`, so a lane added without a refusal path fails at the same moment as any
other broken test rather than waiting for someone to invoke the script by hand.

Precedent: `scripts/check-npm-unlocks.py` + `tests/test_npm_unlock_verification.py`.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from algovault_bot.db import Database
from algovault_bot.quota import (
    FREE_TIER_MONTHLY_QUOTA,
    REFUSAL_LANES,
    evaluate_delivery,
    get_quota_state,
)

GATE = Path(__file__).resolve().parent.parent / "scripts" / "check-quota-refusal-seam.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), *args], capture_output=True, text=True
    )


def test_gate_passes_on_the_current_tree() -> None:
    r = _run()
    assert "QUOTA_REFUSAL_SEAM_VERDICT=PASS" in r.stdout, r.stdout + r.stderr
    assert r.returncode == 0


def test_gate_self_test_passes_both_ways() -> None:
    """The gate must prove it can FAIL, not merely that it can pass."""
    r = _run("--self-test")
    assert "SELF-TEST: PASS" in r.stdout, r.stdout + r.stderr
    assert "QUOTA_REFUSAL_SEAM_VERDICT=PASS" in r.stdout
    assert r.returncode == 0


def test_gate_emits_exactly_one_verdict_token() -> None:
    """Callers gate on the TOKEN, so two tokens is as bad as none."""
    r = _run()
    assert r.stdout.count("QUOTA_REFUSAL_SEAM_VERDICT=") == 1, r.stdout


def test_every_declared_lane_is_reachable_from_the_package() -> None:
    """L2b in the suite: a lane table entry naming a function nobody has is a
    permission slip. This is the shape that let a dark `paywall.py` look wired."""
    src = (Path(__file__).resolve().parent.parent / "src" / "algovault_bot").glob("*.py")
    blob = "\n".join(p.read_text() for p in src)
    for lane in REFUSAL_LANES:
        assert f"def {lane}(" in blob, f"REFUSAL_LANES declares {lane}, which does not exist"


def test_lane_shapes_are_the_two_we_support() -> None:
    assert set(REFUSAL_LANES.values()) <= {"push", "pull"}


# ── the decision itself ──────────────────────────────────────────────────────


@pytest.fixture()
def walled_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    db.upsert_subscriber(1, "walled", "en")
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=?",
            (FREE_TIER_MONTHLY_QUOTA, datetime.now(timezone.utc).isoformat(), 1),
        )
    return db


def test_walled_user_is_refused_and_notice_is_due(walled_db: Database) -> None:
    d = evaluate_delivery(walled_db, 1)
    assert d.allowed is False
    assert d.notify is True, "first refusal of an episode must announce it"


def test_notice_is_not_due_twice_in_one_window(walled_db: Database) -> None:
    walled_db.mark_quota_cta_fired(1, "100", datetime.now(timezone.utc).isoformat())
    d = evaluate_delivery(walled_db, 1)
    assert d.allowed is False
    assert d.notify is False


def test_new_window_re_arms_the_notice(walled_db: Database) -> None:
    """Window-scoped, not time-throttled: the stamp is compared against the window
    it belongs to, so a fresh window re-arms with no timer and no cleanup job."""
    old = "2026-01-01T00:00:00+00:00"
    walled_db.mark_quota_cta_fired(1, "100", old)
    d = evaluate_delivery(walled_db, 1)
    assert d.notify is True, "a stamp older than the current window must not silence it"


def test_paid_tier_is_never_refused(walled_db: Database) -> None:
    with walled_db._cursor() as cur:
        cur.execute("UPDATE subscribers SET linked_tier='starter' WHERE chat_id=1")
    d = evaluate_delivery(walled_db, 1)
    assert d.allowed is True
    assert d.notify is False


def test_mark_quota_cta_fired_100_actually_writes(walled_db: Database) -> None:
    """Regression: '100' used to fall through to a SILENT no-op, so a caller
    stamping the wall wrote nothing and got no error."""
    stamp = datetime.now(timezone.utc).isoformat()
    walled_db.mark_quota_cta_fired(1, "100", stamp)
    assert get_quota_state(walled_db, 1).quota_100_last_fired_at is not None
