"""PRICING-BOT-DELIVERY-METERING-W1 CH4f — drain the plan-debit outbox.

The recorder enqueues a debit locally so a delivery is never blocked by a metering call; this
drains that queue to signal-MCP out of band, and refreshes the local plan MIRROR from every
response so CH5's wall reads a warm, correct local copy.

OUTCOME → ACTION, and each is a deliberate choice:

  CHARGED / ALREADY_CHARGED  stamp sent_at, refresh mirror (source='debit'). ALREADY_CHARGED is a
                             SUCCESS: the server is telling us this exact delivery was already
                             billed, which is the guard working, not a fault.
  REFUSED                    stamp sent_at, refresh mirror. The wall is a business decision, not a
                             transport failure — retrying it forever would be a queue that never
                             drains. Refreshing the mirror here is HOW THE WALL ARMS.
  INDETERMINATE / transport  leave PENDING, attempts += 1, backoff. Never stamp: we do not know
                             whether the charge landed, and stamping would silently forgive it.
  unlinked / 404             stamp sent_at with a terminal reason. A real terminal state, recorded
                             — never a silent drop.

Fail-soft throughout: this runs on a cron, and a metering fault must never wall a paying customer
or crash the drain.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .db import Database, DEFAULT_DB_PATH
from .entitlement_client import consume, read_state
from .quota import PAID_TIERS

log = logging.getLogger(__name__)

BATCH_LIMIT = 200
#: Give up after this many attempts. The row keeps `last_error` and is counted in the digest, so an
#: abandoned debit is VISIBLE — an invisible one would be revenue quietly lost.
#: Backoff is the CRON CADENCE itself: the drain runs every 5 minutes and retries a pending row on
#: each pass, so attempt N is ~5N minutes after the first. No timestamp bookkeeping, and no
#: separate backoff helper to drift out of sync with the schedule that actually governs it.
MAX_ATTEMPTS = 8
#: A mirror older than this is INDETERMINATE to CH5's wall, which SERVES on it (never wall on a
#: measurement we could not take). The poll below exists to keep mirrors inside this window.
STALENESS_MINUTES = 90


def _is_stale(as_of: str | None, now: datetime) -> bool:
    if not as_of:
        return True  # never observed
    try:
        raw = as_of.replace(" ", "T")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - dt) > timedelta(minutes=STALENESS_MINUTES)


def drain_entitlement_debits(
    db_path: str | None = None, dry_run: bool = False
) -> dict[str, int]:
    """Drain pending debits, then warm idle mirrors. Returns a counter dict for the log."""
    db = Database(db_path or DEFAULT_DB_PATH)
    counts = {
        "pending": 0, "charged": 0, "already": 0, "refused": 0,
        "retry": 0, "abandoned": 0, "unlinked": 0, "polled": 0,
    }
    now = datetime.now(timezone.utc)

    for row in db.pending_entitlement_debits(BATCH_LIMIT):
        counts["pending"] += 1
        api_key = row["linked_api_key"] if "linked_api_key" in row.keys() else None
        tier = row["linked_tier"] if "linked_tier" in row.keys() else None

        # Resolved at SEND time, never from the queue row: an unlinked subscriber's queued debits
        # must not charge a revoked key.
        if not api_key or tier not in PAID_TIERS:
            counts["unlinked"] += 1
            if not dry_run:
                db.mark_entitlement_debit_sent(row["id"], last_error="unlinked")
            continue

        if row["attempts"] >= MAX_ATTEMPTS:
            counts["abandoned"] += 1
            if not dry_run:
                db.mark_entitlement_debit_sent(row["id"], last_error=f"abandoned after {row['attempts']} attempts")
            continue

        if dry_run:
            continue

        resp = consume(
            api_key=api_key,
            channel=row["channel"],
            units=int(row["units"]),
            idempotency_key=row["idem_key"],
            kind=row["kind"],
        )

        if resp is None:
            counts["retry"] += 1
            db.bump_entitlement_debit_attempt(row["id"], "transport")
            continue
        if "_http_status" in resp:
            status = resp["_http_status"]
            if status == 404:
                # The key no longer validates — terminal for this row, recorded not dropped.
                counts["unlinked"] += 1
                db.mark_entitlement_debit_sent(row["id"], last_error="key_invalid_404")
            else:
                counts["retry"] += 1
                db.bump_entitlement_debit_attempt(row["id"], f"http_{status}")
            continue

        outcome = str(resp.get("outcome", ""))
        if outcome in ("CHARGED", "ALREADY_CHARGED"):
            counts["charged" if outcome == "CHARGED" else "already"] += 1
            db.mark_entitlement_debit_sent(row["id"], last_error=None)
            db.update_plan_mirror(row["chat_id"], resp, source="debit")
        elif outcome == "REFUSED":
            # The wall. Stamp it — a refusal is settled, not pending — and refresh the mirror,
            # which is what arms CH5's local wall for the next delivery.
            counts["refused"] += 1
            db.mark_entitlement_debit_sent(row["id"], last_error="REFUSED")
            db.update_plan_mirror(row["chat_id"], resp, source="debit")
        else:
            # INDETERMINATE, or an outcome we do not recognise. Stay pending: we do NOT know
            # whether the charge landed, and stamping would silently forgive an unknown.
            counts["retry"] += 1
            db.bump_entitlement_debit_attempt(row["id"], outcome or "unknown_outcome")

    # ── keep idle mirrors warm ───────────────────────────────────────────────
    # A subscriber taking no alerts still needs a fresh mirror: it is what re-opens a wall after
    # the server's period resets, and what keeps CH5 out of its INDETERMINATE branch.
    if not dry_run:
        for sub in db.paid_linked_chat_ids():
            if sub["linked_tier"] not in PAID_TIERS:
                continue
            if not _is_stale(sub["plan_state_as_of"], now):
                continue
            state = read_state(sub["linked_api_key"], "bot")
            if not state or "_http_status" in state:
                continue
            db.update_plan_mirror(sub["chat_id"], state, source="poll")
            counts["polled"] += 1

    log.info('{"event": "entitlement_drain", %s}' % ", ".join(f'"{k}": {v}' for k, v in counts.items()))
    return counts
