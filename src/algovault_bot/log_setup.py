"""Structured-JSON file logging — alerts.log is the canonical alert-event log.

systemd's StandardOutput=journal handles general process logging. We add a
parallel file handler for ``/var/log/algovault-bot/alerts.log`` so the C5
verification gate can ``jq .`` recent alerts and operators have a stable
on-disk audit trail (logrotate weekly × 8 weeks per /etc/logrotate.d/algovault-bot).

Each alert-firing event is logged via the ``alerts_logger()`` helper —
one JSON object per line; ts + event + dimensions.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import WatchedFileHandler
from pathlib import Path

ALERTS_LOG_PATH = "/var/log/algovault-bot/alerts.log"
_alerts_logger: logging.Logger | None = None


class _JsonOnlyFormatter(logging.Formatter):
    """Emit pre-serialized JSON messages without the "asctime LEVELNAME logger" prefix.

    The ``alerts_logger().info(json.dumps({...}))`` callers already produce
    valid JSON; we just write the message verbatim. ``jq .`` on the resulting
    file parses cleanly.
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage().strip()
        # If the caller emitted plain text, wrap it so the file remains 1-JSON-per-line.
        if not (msg.startswith("{") and msg.endswith("}")):
            return json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "level": record.levelname,
                    "msg": msg,
                }
            )
        return msg


def alerts_logger() -> logging.Logger:
    """Return the alerts logger; idempotent (safe to call from any entry point)."""
    global _alerts_logger
    if _alerts_logger is not None:
        return _alerts_logger

    logger = logging.getLogger("algovault_bot.alerts")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't bubble up to journald twice

    # Best-effort file handler — survive read-only mounts / missing dirs without crashing.
    try:
        path = os.environ.get("ALGOVAULT_BOT_ALERTS_LOG", ALERTS_LOG_PATH)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = WatchedFileHandler(path)
    except (OSError, PermissionError):
        handler = logging.NullHandler()
    handler.setFormatter(_JsonOnlyFormatter())
    logger.addHandler(handler)

    _alerts_logger = logger
    return logger


def log_alert_event(event: str, **fields: object) -> None:
    """Emit one structured-JSON line to alerts.log."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    alerts_logger().info(json.dumps(payload, default=str))
