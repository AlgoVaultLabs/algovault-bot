"""PRICING-BOT-DELIVERY-METERING-W1 CH4 — outbox, mirror, and the seam enqueue.

The load-bearing assertion in this file is the FREE-LANE one: this wave unifies the PAID lane with
the server's meter and must not move the free lane by a single byte. Everything else here is
plumbing; that one is the regression guard.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from algovault_bot.db import Database
from algovault_bot.quota import record_call_delivered, record_regime_delivered


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(str(tmp_path / "t.db"))
    d.upsert_subscriber(1, "free", "en")
    d.upsert_subscriber(2, "paid", "en")
    d.link_subscriber(2, "av_live_paidkey", "starter")
    return d


# ── migrations ──────────────────────────────────────────────────────────────


def test_migrations_are_idempotent_across_reinit(tmp_path: Path) -> None:
    p = str(tmp_path / "t.db")
    Database(p)
    Database(p)  # must not raise — AC4
    db = Database(p)
    with db._cursor() as cur:
        cur.execute("PRAGMA table_info(subscribers)")
        cols = {r[1] for r in cur.fetchall()}
    for c in ("plan_used", "plan_total", "plan_allowed", "plan_limit_kind", "plan_period_start",
              "plan_daily_day", "plan_next_json", "plan_state_as_of", "plan_state_source",
              "plan_wall_notice_day",
              # OPS-BOT-LINKED-TIER-REFRESH-W1 CH2 — the tier joins the mirror it belongs to.
              "plan_tier"):
        assert c in cols, f"mirror column {c} missing"


def test_record_alert_fired_returns_a_monotonic_id(db: Database) -> None:
    a = db.record_alert_fired(1, "call", "watch")
    b = db.record_alert_fired(1, "call", "watch")
    assert isinstance(a, int) and a > 0
    assert b > a, "the id must be monotonic — it is the idempotency source"


def test_record_alert_fired_still_validates(db: Database) -> None:
    with pytest.raises(ValueError):
        db.record_alert_fired(1, "not-a-kind", "watch")
    with pytest.raises(ValueError):
        db.record_alert_fired(1, "call", "not-a-source")


# ── the enqueue is PAID-ONLY ────────────────────────────────────────────────


def test_paid_delivery_enqueues_one_debit(db: Database) -> None:
    record_call_delivered(db, 2, "watch")
    rows = db.pending_entitlement_debits()
    assert len(rows) == 1
    assert rows[0]["chat_id"] == 2
    assert rows[0]["channel"] == "bot"
    assert rows[0]["kind"] == "call"
    assert rows[0]["units"] == 1
    assert rows[0]["idem_key"].startswith("bot:2:")


def test_regime_delivery_enqueues_too(db: Database) -> None:
    record_regime_delivered(db, 2, "watch")
    assert db.pending_entitlement_debits()[0]["kind"] == "regime"


def test_free_delivery_enqueues_NOTHING(db: Database) -> None:
    record_call_delivered(db, 1, "watch")
    assert db.count_pending_entitlement_debits() == 0


def test_free_lane_arithmetic_is_byte_identical(db: Database) -> None:
    """AC3 — the free meter must not move. If this fails, the wave broke the lane it promised
    not to touch."""
    for _ in range(5):
        record_call_delivered(db, 1, "watch")
    row = db.get_subscriber(1)
    assert row["alert_count"] == 5
    assert row["alerts_window_start"] is not None
    assert db.count_pending_entitlement_debits() == 0
    # And a paid delivery must not perturb the free subscriber either.
    record_call_delivered(db, 2, "watch")
    assert db.get_subscriber(1)["alert_count"] == 5


def test_paid_subscriber_bot_meter_still_does_not_tick(db: Database) -> None:
    """The bot-side 100/mo counter stays bypassed for paid tiers — CH4 adds the PLAN debit
    alongside it, it does not resurrect the free meter for paying users."""
    record_call_delivered(db, 2, "watch")
    assert db.get_subscriber(2)["alert_count"] == 0


def test_idem_key_uniqueness_rejects_a_duplicate(db: Database) -> None:
    assert db.enqueue_entitlement_debit("bot:2:99", 2, "call") is True
    assert db.enqueue_entitlement_debit("bot:2:99", 2, "call") is False
    assert db.count_pending_entitlement_debits() == 1


def test_enqueue_never_raises_into_the_delivery_path(db: Database) -> None:
    """A bookkeeping fault must never cost a subscriber an alert."""
    with patch.object(Database, "enqueue_entitlement_debit", side_effect=RuntimeError("disk full")):
        record_call_delivered(db, 2, "watch")  # must not raise
    # The delivery itself still landed.
    regime, call = db.count_alerts_fired_last_24h()
    assert call == 1


# ── the drainer ─────────────────────────────────────────────────────────────


def _resp(outcome: str, **over: object) -> dict:
    base = {
        "ok": True, "outcome": outcome, "tier": "starter", "used": 12, "total": 10000,
        "remaining": 9988, "allowed": outcome != "REFUSED", "limit": None,
        "period_start": "2026-08-01T00:00:00.000Z", "daily_day": "2026-08-17",
        "refuses_at_wall": True, "next_plan": {"id": "pro", "monthly_calls": 100000},
    }
    base.update(over)
    return base


@pytest.mark.parametrize("outcome", ["CHARGED", "ALREADY_CHARGED", "REFUSED"])
def test_drainer_stamps_terminal_outcomes_and_refreshes_the_mirror(db: Database, tmp_path: Path, outcome: str) -> None:
    from algovault_bot import entitlement_drain
    record_call_delivered(db, 2, "watch")
    with patch.object(entitlement_drain, "consume", return_value=_resp(outcome)), \
         patch.object(entitlement_drain, "read_state", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)
    assert db.count_pending_entitlement_debits() == 0, f"{outcome} must be terminal"
    row = db.get_subscriber(2)
    assert row["plan_state_as_of"] is not None
    assert row["plan_used"] == 12
    assert row["plan_total"] == 10000
    assert row["plan_state_source"] == "debit"
    # REFUSED is how the wall ARMS — the mirror must record allowed=0.
    assert row["plan_allowed"] == (0 if outcome == "REFUSED" else 1)


def test_drainer_leaves_INDETERMINATE_pending_and_counts_the_attempt(db: Database) -> None:
    from algovault_bot import entitlement_drain
    record_call_delivered(db, 2, "watch")
    with patch.object(entitlement_drain, "consume", return_value=_resp("INDETERMINATE")), \
         patch.object(entitlement_drain, "read_state", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)
    rows = db.pending_entitlement_debits()
    assert len(rows) == 1, "we do not know whether the charge landed — never stamp"
    assert rows[0]["attempts"] == 1


def test_drainer_retries_a_transport_fault(db: Database) -> None:
    from algovault_bot import entitlement_drain
    record_call_delivered(db, 2, "watch")
    with patch.object(entitlement_drain, "consume", return_value=None), \
         patch.object(entitlement_drain, "read_state", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)
    assert db.pending_entitlement_debits()[0]["attempts"] == 1


def test_unlinked_subscriber_terminates_with_a_reason(db: Database) -> None:
    from algovault_bot import entitlement_drain
    record_call_delivered(db, 2, "watch")
    db.unlink_subscriber(2)  # key revoked AFTER the debit was queued
    with patch.object(entitlement_drain, "consume") as c, \
         patch.object(entitlement_drain, "read_state", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)
        c.assert_not_called()  # a revoked key must never be charged
    assert db.count_pending_entitlement_debits() == 0
    with db._cursor() as cur:
        cur.execute("SELECT last_error FROM entitlement_outbox")
        assert cur.fetchone()[0] == "unlinked"


def test_a_stale_mirror_is_polled_warm(db: Database) -> None:
    from algovault_bot import entitlement_drain
    with patch.object(entitlement_drain, "read_state", return_value=_resp("READ")) as rs, \
         patch.object(entitlement_drain, "consume", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)
        rs.assert_called_once()
    assert db.get_subscriber(2)["plan_state_source"] == "poll"


def test_a_fresh_mirror_is_not_polled(db: Database) -> None:
    from algovault_bot import entitlement_drain
    db.update_plan_mirror(2, _resp("READ"), source="poll")
    with patch.object(entitlement_drain, "read_state") as rs, \
         patch.object(entitlement_drain, "consume", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)
        rs.assert_not_called()


def test_uncapped_tier_stores_NULL_not_zero(db: Database) -> None:
    """`total: null` means NO CEILING. Storing 0 would wall an enterprise subscriber instantly."""
    db.update_plan_mirror(2, _resp("CHARGED", total=None, remaining=None), source="debit")
    assert db.get_subscriber(2)["plan_total"] is None


def test_telemetry_counters(db: Database) -> None:
    from algovault_bot import entitlement_drain
    record_call_delivered(db, 2, "watch")
    assert db.count_pending_entitlement_debits() == 1
    with patch.object(entitlement_drain, "consume", return_value=_resp("CHARGED")), \
         patch.object(entitlement_drain, "read_state", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)
    assert db.count_pending_entitlement_debits() == 0
    assert db.count_plan_units_debited_last_24h() == 1
    # A REFUSED row carries a reason and must NOT count as a debit.
    db.enqueue_entitlement_debit("bot:2:777", 2, "call")
    with db._cursor() as cur:
        cur.execute("UPDATE entitlement_outbox SET sent_at=datetime('now'), last_error='REFUSED' WHERE idem_key='bot:2:777'")
    assert db.count_plan_units_debited_last_24h() == 1


# ── OPS-BOT-LINKED-TIER-REFRESH-W1 CH2 — the drainer carries TIER, on the same call ────
#
# 2b's whole claim is that no new transport, call or cadence is needed: `tier` has been in
# the 200 body all along (verified live on both routes 2026-08-21) and was discarded. These
# assert it end-to-end through the drainer rather than through `update_plan_mirror` alone.


@pytest.mark.parametrize("outcome", ["CHARGED", "ALREADY_CHARGED", "REFUSED"])
def test_the_DEBIT_path_writes_plan_tier(db: Database, outcome: str) -> None:
    from algovault_bot import entitlement_drain
    record_call_delivered(db, 2, "watch")
    with patch.object(entitlement_drain, "consume", return_value=_resp(outcome, tier="pro")), \
         patch.object(entitlement_drain, "read_state", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)
    row = db.get_subscriber(2)
    assert row["plan_tier"] == "pro"
    assert row["linked_tier"] == "starter", "the write-once copy is deliberately left alone"


def test_the_IDLE_POLL_path_writes_plan_tier(db: Database) -> None:
    from algovault_bot import entitlement_drain
    with patch.object(entitlement_drain, "read_state", return_value=_resp("READ", tier="pro")), \
         patch.object(entitlement_drain, "consume", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)
    row = db.get_subscriber(2)
    assert row["plan_tier"] == "pro"
    assert row["plan_state_source"] == "poll"


def test_an_upgrade_reaches_the_label_within_one_drain_cycle(db: Database) -> None:
    """Chat 1061466212's defect, reproduced and then closed.

    Linked as `starter`; the server now says `pro`. One ordinary drain pass — no new
    schedule, no new call — and every tier-labelled surface reads Pro.
    """
    from algovault_bot import entitlement_drain
    from algovault_bot.quota import get_quota_state

    assert get_quota_state(db, 2).effective_tier == ("starter", "link")

    record_call_delivered(db, 2, "watch")
    with patch.object(entitlement_drain, "consume", return_value=_resp("CHARGED", tier="pro")), \
         patch.object(entitlement_drain, "read_state", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)

    assert get_quota_state(db, 2).effective_tier == ("pro", "mirror")


def test_a_body_without_tier_does_not_erase_the_last_known_one(db: Database) -> None:
    """Fail-open, same shape as the rest of the mirror: an absent field is UNOBSERVED,
    never a downgrade. The label falls back to `linked_tier` rather than blanking."""
    from algovault_bot import entitlement_drain
    from algovault_bot.quota import get_quota_state

    body = _resp("READ")
    del body["tier"]
    with patch.object(entitlement_drain, "read_state", return_value=body), \
         patch.object(entitlement_drain, "consume", return_value=None):
        entitlement_drain.drain_entitlement_debits(db.path)

    assert db.get_subscriber(2)["plan_tier"] is None
    assert get_quota_state(db, 2).effective_tier == ("starter", "link")
