"""TG-BROADCAST-STACK-W1 CH5 (2026-05-28): X-follow screenshot review queue.

Receives photo uploads from subscribers in ``pending_x_screenshot`` state,
saves to ``/var/lib/algovault-bot/screenshots/<chat_id>-<ts>.jpg``, emits
operator-review DM with [Approve]/[Reject] inline buttons, handles the
callback transitions to ``verified`` (+ tg_pro_grants insert) or back to
``not_started`` (with retry DM).

A separate Hetzner cron (``scripts/check-screenshot-queue.sh``) fires the
CRITICAL_PERSISTENT operator-action alert via ``send_telegram.sh`` wrapper
when ≥1 screenshot has been pending for ≥4h (per spec C5).

Spec reference: ``Prompt/tg-broadcast-stack-w1.md`` Chapter C5.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

DEFAULT_SCREENSHOTS_DIR: Final = Path(
    os.environ.get("ALGOVAULT_SCREENSHOTS_DIR", "/var/lib/algovault-bot/screenshots")
)

# How long a screenshot can sit unreviewed before the operator-alert fires.
QUEUE_REVIEW_SLA_HOURS: Final = 4


def compute_screenshot_path(
    chat_id: int,
    now: datetime | None = None,
    base_dir: Path | None = None,
) -> Path:
    """Deterministic per-chat-id per-timestamp filename. Operator can match
    the file back to subscriber by inspecting the leading chat_id segment.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    base = base_dir or DEFAULT_SCREENSHOTS_DIR
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    return base / f"{chat_id}-{ts}.jpg"


def is_pending_x_screenshot(unlock_status: str | None) -> bool:
    """True when the subscriber tapped [Follow X] but has not yet uploaded
    a screenshot OR is awaiting operator review.
    """
    return unlock_status == "pending_x_screenshot"


def screenshot_age_hours(path: Path, now: datetime | None = None) -> float | None:
    """Wall-clock age of the screenshot file in hours, via filesystem mtime.

    Returns None if the file does not exist (subscriber state column may
    point at a path the operator manually deleted; treat as no-queue).
    """
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - mtime).total_seconds() / 3600.0


def format_operator_review_caption(
    chat_id: int,
    username: str | None,
    lang_code: str | None,
) -> str:
    """Caption attached to the operator-DM photo. Used to identify the
    subscriber + their language context for the eventual approve/reject DM.
    """
    handle = f"@{username}" if username else f"chat_id={chat_id}"
    lang = lang_code or "unknown"
    return (
        f"📸 TG-UNLOCK screenshot from {handle} ({lang})\n"
        f"chat_id={chat_id}\n"
        f"Pending X-follow verification.\n"
        f"Tap [Approve] to grant 30 days Pro · [Reject] to request a clearer screenshot."
    )


def format_queue_alert_body(
    pending_count: int,
    oldest_age_hours: float,
) -> str:
    """Body for the send_telegram.sh wrapper CRITICAL_PERSISTENT alert when
    the operator review queue is non-empty for ≥4h. Recommended-wave template
    stays in `W{NEXT}` form (per CLAUDE.md `Monitoring recommended_wave template`).
    """
    return (
        f"🛑 TG_UNLOCK_SCREENSHOT_QUEUE_PENDING\n"
        f"Pending screenshots: {pending_count}\n"
        f"Oldest age: {oldest_age_hours:.1f}h\n"
        f"\n"
        f"Action: review pending screenshots in operator-DM channel\n"
        f"  → tap [Approve] to grant 30 days Pro\n"
        f"  → tap [Reject] to request retry from subscriber\n"
        f"\n"
        f"Source: /var/lib/algovault-bot/screenshots/\n"
        f"Recommended wave (if queue chronically non-empty): "
        f"OPS-TG-UNLOCK-SCREENSHOT-AUTOMATION-W{{NEXT}}"
    )
