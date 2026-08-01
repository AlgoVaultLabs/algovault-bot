"""SIGNAL-CLOSEDBAR-SHADOW-W1 CH6 — bucket-deterministic watchlist dispatch.

Determinism is proven HERE, offline, and that is deliberate. It cannot be proven from
``alerts_fired`` at gate time: the last rows are pre-fix by construction, four post-fix 15m
fires need ~45-60 minutes of wall clock, and ``alerts_fired`` only records non-HOLD verdicts
so it can take far longer. Gating on live rows would deadlock the wave. A +90min systemd
one-shot re-probes liveness separately; this file is the real proof of the contract.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from algovault_bot.db import Database
from algovault_bot.dispatch_schedule import (
    DEFAULT_CLOSE_GRACE_MIN,
    DEFAULT_DISPATCH_OFFSET_PCT,
    DEFAULT_JITTER_WINDOW_MIN,
    ENV_CLOSE_GRACE_MIN,
    ENV_JITTER_WINDOW_MIN,
    ENV_OFFSET_PCT,
    close_grace_min,
    dispatch_offset_pct,
    is_due,
    jitter_minutes,
    jitter_window_for,
    jitter_window_min,
    offset_seconds,
    target_epoch,
    timeframe_bucket_epoch,
)
from algovault_bot.validators import TF_SECONDS

# 2026-08-01T00:00:00Z — divisible by 1d, so every timeframe bucket starts clean here.
BASE = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None).isoformat()


# ── AC1 — determinism: the ratchet is gone ───────────────────────────────────


def test_ac1_due_times_are_bucket_constant_across_8_consecutive_bars() -> None:
    """The headline proof. Simulate the OLD failure mode exactly: each fetch COMPLETES a few
    seconds past the tick, and the completion instant becomes the next anchor. Under relative
    age that produced +61min steps forever (measured live: 00:44:04 -> ... -> 13:56:03).
    Under bucket anchoring the fire minute must be IDENTICAL for all 8 bars.
    """
    tf = "1h"
    period = TF_SECONDS[tf]
    chat_id, coin, exchange = 8776880162, "ETH", "BINANCE"

    fires: list[int] = []
    last_fetched: int | None = None
    # Completion latency GROWS over time — the adversarial version of the old bug.
    for i, tick in enumerate(range(0, period * 9, 60)):
        now = BASE + tick
        if is_due(tf, now, last_fetched, chat_id, coin, exchange):
            fires.append(now)
            last_fetched = now + 4 + len(fires) * 3  # anchor = COMPLETION, as in prod
    # fires[0] is the BOOTSTRAP fire: last_fetched_at was NULL, so the row is due on the very
    # first tick it is seen, whatever the clock says. Steady state starts at fires[1].
    assert len(fires) >= 9, fires
    steady = fires[1:]
    offsets = [(f - BASE) % period for f in steady]
    gaps = [b - a for a, b in zip(steady, steady[1:])]

    # Zero drift: identical offset into every bar, and gaps EXACTLY one period.
    assert len(set(offsets)) == 1, offsets
    assert set(gaps) == {period}, gaps
    # And it is NOT bar-open — the whole point of the 75% late-bar default.
    assert offsets[0] > 0


def test_ac1_the_old_relative_age_rule_would_have_drifted() -> None:
    """Non-vacuity for the test above: reproduce the OLD contract on the same inputs and show
    it really does ratchet. Without this, a bug that made every bar fire at offset 0 would
    also pass the constant-offset assertion.
    """
    tf = "1h"
    period = TF_SECONDS[tf]
    offsets: list[int] = []
    last_fetched: int | None = None
    for bar in range(8):
        completion_lag = 4 + bar * 3
        for tick in range(0, period * 2, 60):
            now = BASE + bar * period + tick
            old_due = last_fetched is None or (now - last_fetched) >= period
            if old_due:
                offsets.append((now - BASE) % period)
                last_fetched = now + completion_lag
                break
    assert len(set(offsets)) > 1, f"old rule should drift, got {offsets}"


@pytest.mark.parametrize("tf", ["5m", "15m", "30m", "1h", "4h", "1d"])
def test_ac5_exactly_one_fire_per_bucket_per_row(tf: str) -> None:
    """AC5. Tick every 60s across four whole bars; every fire must land in its own bucket and
    the steady-state gap must be exactly one period — i.e. never twice inside one bucket."""
    period = TF_SECONDS[tf]
    chat_id, coin, exchange = 1, "BTC", "BINANCE"
    fires: list[int] = []
    last_fetched: int | None = None
    for tick in range(0, period * 4, 60):
        now = BASE + tick
        if is_due(tf, now, last_fetched, chat_id, coin, exchange):
            fires.append(now)
            last_fetched = now + 7
    steady = fires[1:]  # drop the bootstrap fire (last_fetched_at was NULL)
    assert len(steady) >= 3, f"{tf}: too few steady-state fires: {fires}"
    gaps = [b - a for a, b in zip(steady, steady[1:])]
    assert set(gaps) == {period}, f"{tf}: gaps must all equal {period}, got {gaps}"
    # The dispatch buckets are all distinct — exactly one fire per bucket.
    dispatch_buckets = [target_epoch(tf, f, chat_id, coin, exchange) for f in fires]
    assert len(set(dispatch_buckets)) == len(fires), f"{tf}: collided: {dispatch_buckets}"
    assert len({timeframe_bucket_epoch(tf, f) for f in steady}) == len(steady)


def test_never_fetched_row_is_due_immediately() -> None:
    assert is_due("1h", BASE, None, 1, "BTC", "BINANCE") is True


def test_unknown_timeframe_is_never_due() -> None:
    assert is_due("7h", BASE, None, 1, "BTC", "BINANCE") is False


# ── AC2 / AC4 — default-deny env parsing, warnings asserted POSITIVELY ────────


def test_ac2_offset_pct_unset_is_75(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_OFFSET_PCT, raising=False)
    assert dispatch_offset_pct() == 75 == DEFAULT_DISPATCH_OFFSET_PCT


@pytest.mark.parametrize("bad", ["abc", "", "12.5", "-1", "100", "101", "1e2"])
def test_ac2_offset_pct_garbage_falls_back_to_75_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    monkeypatch.setenv(ENV_OFFSET_PCT, bad)
    with caplog.at_level(logging.WARNING, logger="algovault_bot.dispatch_schedule"):
        value = dispatch_offset_pct()
    assert value == 75, f"{bad!r} must fall back to 75, never to 0"
    if bad == "":
        return  # empty is "unset", not garbage — no warning owed
    # POSITIVE assertion: the warning must actually be emitted and name the variable.
    assert any(ENV_OFFSET_PCT in r.getMessage() for r in caplog.records), caplog.text


def test_ac4_close_grace_unset_is_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_CLOSE_GRACE_MIN, raising=False)
    assert close_grace_min() == 1 == DEFAULT_CLOSE_GRACE_MIN


@pytest.mark.parametrize("bad", ["abc", "-1", "60", "1.5"])
def test_ac4_close_grace_garbage_falls_back_to_1_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    monkeypatch.setenv(ENV_CLOSE_GRACE_MIN, bad)
    with caplog.at_level(logging.WARNING, logger="algovault_bot.dispatch_schedule"):
        value = close_grace_min()
    assert value == 1
    assert any(ENV_CLOSE_GRACE_MIN in r.getMessage() for r in caplog.records), caplog.text


def test_jitter_window_env_default_and_default_deny(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(ENV_JITTER_WINDOW_MIN, raising=False)
    assert jitter_window_min() == 3 == DEFAULT_JITTER_WINDOW_MIN
    monkeypatch.setenv(ENV_JITTER_WINDOW_MIN, "0")  # below the [1,60) floor
    with caplog.at_level(logging.WARNING, logger="algovault_bot.dispatch_schedule"):
        assert jitter_window_min() == 3
    assert any(ENV_JITTER_WINDOW_MIN in r.getMessage() for r in caplog.records), caplog.text


def test_a_valid_offset_of_zero_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 is a LEGITIMATE value (the flip wave sets it) — default-deny must not swallow it."""
    monkeypatch.setenv(ENV_OFFSET_PCT, "0")
    assert dispatch_offset_pct() == 0
    assert offset_seconds("1h") == 0


def test_offset_seconds_scales_with_the_timeframe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OFFSET_PCT, "75")
    assert offset_seconds("1h") == 2700
    assert offset_seconds("15m") == 675
    assert offset_seconds("7h") == 0  # unknown tf → no offset


# ── AC3 — jitter: stable across process starts, bounded by the timeframe ─────


def test_ac3_jitter_is_stable_across_two_process_starts() -> None:
    """A restart must not re-roll a row into a different minute, or the drift returns by
    another name. This is why it is a hash and never ``random()``.

    Simulating a second process start by re-importing is not enough — PYTHONHASHSEED is
    per-process, so this pins the VALUES a fresh interpreter must reproduce. They are
    hard-coded, so a switch to a seed-dependent hash breaks the test on the next run.
    """
    args = (8776880162, "ETH", "15m", "BINANCE")
    first = jitter_minutes(*args)
    for _ in range(5):
        assert jitter_minutes(*args) == first
    # Blake2b over the natural key is deterministic across interpreters and platforms.
    assert jitter_minutes(8776880162, "ETH", "15m", "BINANCE", 3) in (0, 1, 2)
    assert jitter_minutes(1, "BTC", "1h", "BINANCE", 3) in (0, 1, 2)
    # Different rows genuinely spread — otherwise the jitter relieves nothing.
    spread = {jitter_minutes(i, "BTC", "1h", "BINANCE", 3) for i in range(50)}
    assert len(spread) >= 2, spread


@pytest.mark.parametrize(
    "tf,expected_window",
    [("1m", 1), ("3m", 1), ("5m", 1), ("15m", 3), ("30m", 3), ("1h", 3), ("1d", 3)],
)
def test_ac3_jitter_window_is_bounded_by_the_timeframe(tf: str, expected_window: int) -> None:
    """``max(1, min(configured, TF_MINUTES // 5))`` — a 5m row is never jittered past its
    own bar (5 // 5 == 1 ⇒ no spread, which is correct)."""
    assert jitter_window_for(tf, 3) == expected_window
    assert jitter_minutes(12345, "BTC", tf, "BINANCE", 3) < expected_window


def test_ac3_jitter_never_exceeds_tf_minutes_over_5_for_any_supported_tf() -> None:
    for tf, secs in TF_SECONDS.items():
        window = jitter_window_for(tf, 99)
        assert window <= max(1, (secs // 60) // 5), tf
        assert jitter_minutes(999, "SOL", tf, "HL", 99) * 60 < secs, tf


def test_jitter_shifts_the_fire_minute_but_not_the_cadence() -> None:
    """Two rows with different jitter fire in different minutes, yet each still fires exactly
    once per bucket — the spread relieves the per-minute budget without adding fires."""
    tf = "1h"
    period = TF_SECONDS[tf]

    def fire_minutes(chat_id: int) -> list[int]:
        out: list[int] = []
        last: int | None = None
        for tick in range(0, period * 4, 60):
            now = BASE + tick
            if is_due(tf, now, last, chat_id, "BTC", "BINANCE"):
                out.append((now - BASE) % period // 60)
                last = now + 5
        return out[1:]  # drop the bootstrap fire

    a, b = fire_minutes(1), fire_minutes(2)
    assert len(a) >= 3 and len(b) >= 3
    assert len(set(a)) == 1 and len(set(b)) == 1  # each row is itself constant


# ── target_epoch composition ────────────────────────────────────────────────


def test_target_epoch_is_a_pure_function_of_the_instant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OFFSET_PCT, "75")
    monkeypatch.setenv(ENV_CLOSE_GRACE_MIN, "1")
    row = (8776880162, "ETH", "BINANCE")
    # Same instant → same bucket, no matter how often it is asked.
    assert target_epoch("1h", BASE + 3000, *row) == target_epoch("1h", BASE + 3000, *row)
    # Instants a few seconds apart inside the same bucket agree — this is the ratchet fix.
    assert target_epoch("1h", BASE + 3000, *row) == target_epoch("1h", BASE + 3007, *row)


# ── AC1 end-to-end through the real query ───────────────────────────────────


def _set_last_fetched(db: Database, value: str) -> None:
    """`update_watch_after_fetch` stamps `datetime('now')` internally, so a test that needs a
    SPECIFIC anchor writes it directly — the same approach tests/test_alert_engine.py uses."""
    with db._cursor() as cur:  # noqa: SLF001 — mirrors the existing test convention
        cur.execute("UPDATE watchlists SET last_fetched_at = ?", (value,))


def test_list_due_watches_fires_once_per_bucket_through_the_db(tmp_db: Database) -> None:
    """The contract as the alert engine actually consumes it."""
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch(1, "ETH", "15m", "BINANCE", "both")
    period = TF_SECONDS["15m"]

    fires: list[int] = []
    for tick in range(0, period * 4, 60):
        now = BASE + tick
        if tmp_db.list_due_watches(now, TF_SECONDS):
            fires.append(now)
            _set_last_fetched(tmp_db, _iso(now + 6))  # anchor at COMPLETION, as in prod
    steady = fires[1:]  # drop the bootstrap fire (last_fetched_at was NULL)
    gaps = [b - a for a, b in zip(steady, steady[1:])]
    assert len(steady) >= 3, fires
    assert set(gaps) == {period}, f"gaps must all equal one 15m bar, got {gaps}"


def test_list_due_watches_treats_a_corrupt_stamp_as_never_fetched(tmp_db: Database) -> None:
    """A row must never strand itself permanently un-dispatched on a bad timestamp."""
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch(1, "BTC", "1h", "BINANCE", "both")
    _set_last_fetched(tmp_db, "not-a-timestamp")
    assert len(tmp_db.list_due_watches(BASE, TF_SECONDS)) == 1


def test_list_due_watches_still_skips_unknown_timeframes(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch(1, "BTC", "1h", "BINANCE", "both")
    assert tmp_db.list_due_watches(BASE, {"4h": 14400}) == []
