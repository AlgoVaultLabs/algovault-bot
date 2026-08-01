"""Bucket-deterministic watchlist dispatch — SIGNAL-CLOSEDBAR-SHADOW-W1 CH6.

``list_due_watches`` used to mark a row due on RELATIVE age (``now - last_fetched_at >=
TF_SECONDS``) and re-stamp the anchor at fetch COMPLETION, which happens seconds past the
``OnCalendar=*:*:00`` tick. So every fire slipped later than the last one, forever. Measured
on the live box: ``00:44:04 -> 01:45:07 -> ... -> 13:56:03`` — exact +61min steps on a 1h row.

The same repository already contained the correct primitive: ``timeframe_bucket_epoch``,
used by ``/scanwatch``, whose fires land cleanly on 14:30 / 14:45 / 15:00. Two schedulers,
two contracts, one codebase. This module is the single derivation both now share.

``timeframe_bucket_epoch`` LIVES HERE and ``scan_digest`` re-exports it, so every existing
importer keeps working. This module is a leaf: it imports only ``validators`` (for the
canonical ``TF_SECONDS``), so the storage layer can import it with no cycle.

── The contract ─────────────────────────────────────────────────────────────
    due iff  target_epoch(tf, now) > target_epoch(tf, last_fetched_at)

    target_epoch(tf, t) = bucket_epoch(tf, t - offset_seconds(tf)
                                            - grace_seconds
                                            - jitter_seconds)

Anchoring on a BUCKET rather than an age is what removes the ratchet: the bucket a moment
falls into is a function of the moment alone, so a fetch that completes at :04 and one that
completes at :07 map to the same bucket and produce the same next due-time. No new column
and no migration — the row self-aligns on its first cycle.

── Why jitter is in WHOLE MINUTES ───────────────────────────────────────────
The scheduler is ``OnCalendar=*:*:00``, a 60-SECOND tick, and ``FETCH_BUDGET_PER_MIN`` is a
PER-MINUTE budget. Sub-minute jitter cannot move a row into a different tick and would
therefore relieve nothing. It is also a stable HASH, never ``random()``: a restart must not
re-roll a row into a different minute, or the ratchet returns by another name.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Final

from .validators import TF_SECONDS

logger = logging.getLogger(__name__)

# Late-bar by default: dispatch 75% of the way through the bar. The FLIP wave moves this to
# 0 (true bar-close delivery) with one env change, once the shadow window supports it.
DEFAULT_DISPATCH_OFFSET_PCT: Final[int] = 75
# A closed bar is not published at T+0. At offset=0 this lands dispatch on the :01:00 tick —
# the first tick AFTER close. (A :00:30 target is unreachable at a 60s tick; this is the
# minute-resolution equivalent of Freqtrade opening trades "a few seconds after candle open".)
DEFAULT_CLOSE_GRACE_MIN: Final[int] = 1
DEFAULT_JITTER_WINDOW_MIN: Final[int] = 3

ENV_OFFSET_PCT: Final[str] = "ALGOVAULT_BOT_DISPATCH_OFFSET_PCT"
ENV_CLOSE_GRACE_MIN: Final[str] = "ALGOVAULT_BOT_CLOSE_GRACE_MIN"
ENV_JITTER_WINDOW_MIN: Final[str] = "ALGOVAULT_BOT_JITTER_WINDOW_MIN"


def timeframe_bucket_epoch(timeframe: str, now_sec: int) -> int:
    """TG-SCANWATCH-TF-CADENCE-W1 (Approach B): `now_sec` floored to the TIMEFRAME period —
    the scanwatch re-scan bucket. Dispatch cadence == the subscription's OWN timeframe (no
    coarsening) — a 5m scanwatch re-scans every 5m, a 1h hourly. Leaves cadence_for_timeframe
    (the MCP scan-digest.ts mirror) + the 1h/4h/1d cadence column untouched. Unknown tf → 1d
    floor (conservative; matches cadence_for_timeframe's unknown-tf default).

    Moved here from ``scan_digest`` by SIGNAL-CLOSEDBAR-SHADOW-W1 CH6 so the watchlist
    dispatcher and the scanwatch dispatcher share ONE derivation. Behaviour is unchanged and
    ``scan_digest`` re-exports it; this docstring is deliberately preserved verbatim.
    """
    period = TF_SECONDS.get(timeframe, 86_400)
    return (now_sec // period) * period


def _bounded_int_env(name: str, default: int, lo: int, hi_exclusive: int) -> int:
    """DEFAULT-DENY parse. Non-numeric, or outside ``[lo, hi_exclusive)``, falls back to the
    DEFAULT and logs a warning — never to 0.

    Falling back to 0 would be the dangerous direction for the offset: it means "dispatch at
    bar OPEN", which is precisely the degenerate zone this wave exists to move delivery out
    of. A silent 0 would look like a successful flip while being the opposite of one.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not an integer — falling back to %d", name, raw, default
        )
        return default
    if not (lo <= value < hi_exclusive):
        logger.warning(
            "%s=%d is outside [%d,%d) — falling back to %d", name, value, lo, hi_exclusive, default
        )
        return default
    return value


def dispatch_offset_pct() -> int:
    """How far into the bar to dispatch, as a percentage. ``[0,100)``; default 75."""
    return _bounded_int_env(ENV_OFFSET_PCT, DEFAULT_DISPATCH_OFFSET_PCT, 0, 100)


def close_grace_min() -> int:
    """Minutes of grace after the target instant. ``[0,60)``; default 1."""
    return _bounded_int_env(ENV_CLOSE_GRACE_MIN, DEFAULT_CLOSE_GRACE_MIN, 0, 60)


def jitter_window_min() -> int:
    """Configured jitter width in minutes, before the per-timeframe bound. ``[1,60)``."""
    return _bounded_int_env(ENV_JITTER_WINDOW_MIN, DEFAULT_JITTER_WINDOW_MIN, 1, 60)


def offset_seconds(timeframe: str, pct: int | None = None) -> int:
    """``TF_SECONDS[tf] * pct / 100``, floored. Unknown timeframe → 0 (no offset)."""
    tf_sec = TF_SECONDS.get(timeframe)
    if tf_sec is None:
        return 0
    return (tf_sec * (dispatch_offset_pct() if pct is None else pct)) // 100


def jitter_window_for(timeframe: str, configured: int | None = None) -> int:
    """``max(1, min(configured, TF_MINUTES // 5))``.

    Bounded BY THE TIMEFRAME so a 5m row is never jittered past its own bar: a 5m bar is 5
    minutes, ``5 // 5 == 1``, so its window is exactly one minute — i.e. no spread at all,
    which is correct. Without this bound a 3-minute window would routinely push a 5m row
    into the following bar and re-create the drift in a new form.
    """
    cfg = jitter_window_min() if configured is None else configured
    tf_sec = TF_SECONDS.get(timeframe)
    if tf_sec is None:
        return 1
    return max(1, min(cfg, (tf_sec // 60) // 5))


def jitter_minutes(
    chat_id: int, coin: str, timeframe: str, exchange: str, configured: int | None = None
) -> int:
    """Deterministic per-row spread, in whole minutes.

    A STABLE hash, never ``random()`` — the value has to survive a process restart, or two
    restarts in a bar would dispatch the same row twice. blake2b over the natural key rather
    than ``hash()``, whose seed is randomised per process by PYTHONHASHSEED.
    """
    window = jitter_window_for(timeframe, configured)
    if window <= 1:
        return 0
    key = f"{chat_id}|{coin}|{timeframe}|{exchange}".encode()
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return int.from_bytes(digest, "big") % window


def target_epoch(
    timeframe: str,
    t_sec: int,
    chat_id: int,
    coin: str,
    exchange: str,
    *,
    pct: int | None = None,
    grace_min: int | None = None,
    jitter_window: int | None = None,
) -> int:
    """The bucket that instant ``t_sec`` belongs to for THIS row's dispatch schedule.

    Shifting the instant BACK by (offset + grace + jitter) before flooring is what makes the
    boundary land late in the bar instead of at its open, while keeping the result a pure
    function of ``t_sec`` — which is the whole point.
    """
    grace = (close_grace_min() if grace_min is None else grace_min) * 60
    jitter = jitter_minutes(chat_id, coin, timeframe, exchange, jitter_window) * 60
    shifted = t_sec - offset_seconds(timeframe, pct) - grace - jitter
    return timeframe_bucket_epoch(timeframe, shifted)


def is_due(
    timeframe: str,
    now_sec: int,
    last_fetched_epoch: int | None,
    chat_id: int,
    coin: str,
    exchange: str,
    *,
    pct: int | None = None,
    grace_min: int | None = None,
    jitter_window: int | None = None,
) -> bool:
    """``target_epoch(tf, now) > target_epoch(tf, last_fetched_at)``; never-fetched is due.

    Strictly greater, so a row fires at most ONCE per bucket no matter how many ticks land
    inside it — the property the old age-based check could not express.
    """
    if timeframe not in TF_SECONDS:
        return False
    if last_fetched_epoch is None:
        return True
    kw = {"pct": pct, "grace_min": grace_min, "jitter_window": jitter_window}
    return target_epoch(timeframe, now_sec, chat_id, coin, exchange, **kw) > target_epoch(
        timeframe, last_fetched_epoch, chat_id, coin, exchange, **kw
    )
