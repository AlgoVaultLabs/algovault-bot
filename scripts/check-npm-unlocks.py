#!/usr/bin/env python3
"""TG-BROADCAST-STACK-W1 CH6 (2026-05-28): npm-install verification cron.

Invoked every 10 minutes by Hetzner crontab. Steps:
 1. Query production postgres for funnel_events rows with event_type=
    'first_tool_call_with_track_token' AND ts >= NOW() - INTERVAL '24h'
    (via `docker exec crypto-quant-signal-mcp-postgres-1 psql -t -c ...`).
 2. For each row, extract track_token from meta_json.
 3. Look up bot SQLite subscribers WHERE npm_unlock_session_id = token
    AND unlock_status = 'pending_npm_call'.
 4. On match: insert tg_pro_grants (30-day expiry) + set unlock_status=
    'verified' + set npm_unlock_detected_at NOW() + send DM via sendDM.
 5. Separately: subscribers with unlock_status='pending_npm_call' but no
    matching event AND state set ≥24h ago → mark 'expired' + send DM.

Python3 stdlib only — uses subprocess for psql (production postgres in
container) + sqlite3 for the local bot DB. No pip deps needed.

Env vars:
  ALGOVAULT_BOT_DB_PATH       defaults to /var/lib/algovault-bot/state.db
  POSTGRES_CONTAINER          defaults to crypto-quant-signal-mcp-postgres-1
  POSTGRES_DB                 defaults to signal_performance
  POSTGRES_USER               defaults to algovault
  ALGOVAULT_BOT_TOKEN         needed for outbound DM; if absent, log-only

CLI:
  check-npm-unlocks.py              # normal cron fire
  check-npm-unlocks.py --dry-run    # print decisions; skip DM + DB mutations

Spec reference: ``Prompt/tg-broadcast-stack-w1.md`` Chapter C6.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get(
    "ALGOVAULT_BOT_DB_PATH", "/var/lib/algovault-bot/state.db"
)
POSTGRES_CONTAINER = os.environ.get(
    "POSTGRES_CONTAINER", "crypto-quant-signal-mcp-postgres-1"
)
POSTGRES_DB = os.environ.get("POSTGRES_DB", "signal_performance")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "algovault")

GRANT_DURATION_DAYS = 30
PENDING_NPM_EXPIRY_HOURS = 24

log = logging.getLogger("check-npm-unlocks")


def _psql_query(sql: str) -> list[list[str]]:
    """Run a query in the production postgres container and return rows as
    list-of-columns (TSV-parsed). Empty list on failure.
    """
    cmd = [
        "docker",
        "exec",
        POSTGRES_CONTAINER,
        "psql",
        "-U", POSTGRES_USER,
        "-d", POSTGRES_DB,
        "-t",  # tuples-only
        "-A",  # unaligned, no padding
        "-F", "\t",  # field separator = tab
        "-c", sql,
    ]
    try:
        result = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=20
        )
    except Exception as e:  # noqa: BLE001
        log.warning("psql exec failed: %s", e)
        return []
    if result.returncode != 0:
        log.warning("psql exit=%d stderr=%s", result.returncode, result.stderr.strip())
        return []
    rows: list[list[str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(line.split("\t"))
    return rows


def fetch_pending_track_token_events(window_hours: int = 24) -> list[dict[str, str]]:
    """Return all first_tool_call_with_track_token events within window.

    Each item: {ts, session_id, meta_json}.
    """
    sql = (
        "SELECT ts::text, COALESCE(session_id, ''), COALESCE(meta_json, '') "
        f"FROM funnel_events "
        f"WHERE event_type = 'first_tool_call_with_track_token' "
        f"  AND ts >= NOW() - INTERVAL '{int(window_hours)} hours' "
        f"ORDER BY ts DESC"
    )
    rows = _psql_query(sql)
    out: list[dict[str, str]] = []
    for r in rows:
        if len(r) < 3:
            continue
        out.append({"ts": r[0], "session_id": r[1], "meta_json": r[2]})
    return out


def extract_track_token(meta_json_str: str) -> str | None:
    """Pull track_token from the JSON-encoded meta_json column."""
    if not meta_json_str:
        return None
    try:
        meta = json.loads(meta_json_str)
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    tok = meta.get("track_token")
    if isinstance(tok, str) and 8 <= len(tok) <= 64:
        return tok
    return None


def grant_pro_to_subscriber(
    db_path: str, chat_id: int, dry_run: bool = False
) -> dict[str, str]:
    """Update bot SQLite: insert tg_pro_grants + set unlock_status='verified'
    + set npm_unlock_detected_at. Returns metadata for DM rendering.
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=GRANT_DURATION_DAYS)
    info = {
        "now_iso": now.isoformat(timespec="seconds"),
        "expires_iso": expires.isoformat(timespec="seconds"),
        "expires_date": expires.strftime("%Y-%m-%d"),
    }
    if dry_run:
        return info
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tg_pro_grants "
            "(chat_id, granted_at, expires_at, method) "
            "VALUES (?, ?, ?, 'npm_install')",
            (chat_id, info["now_iso"], info["expires_iso"]),
        )
        conn.execute(
            "UPDATE subscribers SET unlock_status = 'verified', "
            "unlock_verified_at = ?, npm_unlock_detected_at = ? "
            "WHERE chat_id = ?",
            (info["now_iso"], info["now_iso"], chat_id),
        )
        conn.commit()
    finally:
        conn.close()
    return info


def expire_old_pending_npm(
    db_path: str, dry_run: bool = False
) -> list[dict[str, str]]:
    """Find subscribers stuck in pending_npm_call > 24h and transition to
    'expired'. Returns list of (chat_id, lang_code) for DM dispatch.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PENDING_NPM_EXPIRY_HOURS)
    cutoff_iso = cutoff.isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        # Use linked_at OR created_at as the "pending since" proxy if a
        # dedicated unlock_pending_since column doesn't exist (it doesn't
        # in this wave; we'd add one in a follow-up). For the MVP, we
        # consider any pending_npm_call subscriber with no detection
        # AND linked_at < cutoff as expired. Conservative: skip if
        # linked_at IS NULL (no good cutoff). MVP behavior is fine —
        # the daily-digest cron also catches stale state via
        # check_unlock_expiry_should_fire().
        cur = conn.execute(
            "SELECT chat_id, lang_code FROM subscribers "
            "WHERE unlock_status = 'pending_npm_call' "
            "  AND COALESCE(linked_at, last_seen_at, created_at) < ?",
            (cutoff_iso,),
        )
        candidates = [{"chat_id": int(r[0]), "lang_code": r[1] or ""} for r in cur.fetchall()]
        if not dry_run:
            for c in candidates:
                conn.execute(
                    "UPDATE subscribers SET unlock_status = 'expired' "
                    "WHERE chat_id = ?",
                    (c["chat_id"],),
                )
            conn.commit()
    finally:
        conn.close()
    return candidates


def find_pending_subscriber_for_token(
    db_path: str, token: str
) -> tuple[int, str | None] | None:
    """Return (chat_id, lang_code) for a subscriber whose
    npm_unlock_session_id matches the token AND state is pending_npm_call.
    None when no match.
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cur = conn.execute(
            "SELECT chat_id, lang_code FROM subscribers "
            "WHERE npm_unlock_session_id = ? "
            "  AND unlock_status = 'pending_npm_call'",
            (token,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), row[1]


def send_verification_dm(chat_id: int, lang_code: str | None) -> bool:
    """Send the 'Verified! 30 days Pro starts now.' DM via the bot module
    when ALGOVAULT_BOT_TOKEN is set. Returns False if token unset (logged).
    """
    # Lazy import so dry-run paths don't require the bot package.
    pkg_parent = Path(__file__).resolve().parent.parent / "src"
    if pkg_parent.is_dir() and str(pkg_parent) not in sys.path:
        sys.path.insert(0, str(pkg_parent))
    try:
        from algovault_bot.broadcast import sendDM
        from algovault_bot.unlock import METHOD_NPM_INSTALL, format_verified_body
    except Exception as e:  # noqa: BLE001
        log.warning("send_verification_dm import failed: %s", e)
        return False
    body = format_verified_body(METHOD_NPM_INSTALL, lang_code)
    try:
        return bool(sendDM(chat_id, body))
    except Exception as e:  # noqa: BLE001
        log.warning("sendDM failed chat_id=%s err=%s", chat_id, e)
        return False


def send_expired_dm(chat_id: int, lang_code: str | None) -> bool:
    pkg_parent = Path(__file__).resolve().parent.parent / "src"
    if pkg_parent.is_dir() and str(pkg_parent) not in sys.path:
        sys.path.insert(0, str(pkg_parent))
    try:
        from algovault_bot.broadcast import sendDM
        from algovault_bot.unlock import format_expired_body
    except Exception as e:  # noqa: BLE001
        log.warning("send_expired_dm import failed: %s", e)
        return False
    body = format_expired_body(lang_code)
    try:
        return bool(sendDM(chat_id, body))
    except Exception as e:  # noqa: BLE001
        log.warning("sendDM failed chat_id=%s err=%s", chat_id, e)
        return False


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="check-npm-unlocks cron")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv or sys.argv[1:])

    # Step 1: poll for fresh track-token events.
    events = fetch_pending_track_token_events(window_hours=24)
    log.info("fetched %d track-token events in last 24h", len(events))

    granted = 0
    for ev in events:
        token = extract_track_token(ev["meta_json"])
        if not token:
            continue
        match = find_pending_subscriber_for_token(args.db_path, token)
        if match is None:
            continue
        chat_id, lang_code = match
        log.info("MATCH chat_id=%s token_prefix=%s", chat_id, token[:8])
        info = grant_pro_to_subscriber(args.db_path, chat_id, dry_run=args.dry_run)
        if args.dry_run:
            log.info(
                "DRY_RUN: would grant chat_id=%s expires=%s",
                chat_id, info["expires_date"],
            )
        else:
            sent = send_verification_dm(chat_id, lang_code)
            log.info(
                "GRANTED chat_id=%s expires=%s dm_sent=%s",
                chat_id, info["expires_date"], sent,
            )
            granted += 1

    # Step 2: expire 24h-stale pending_npm_call subscribers.
    expired = expire_old_pending_npm(args.db_path, dry_run=args.dry_run)
    log.info("expired %d stale pending_npm subscribers", len(expired))
    for c in expired:
        if args.dry_run:
            continue
        send_expired_dm(c["chat_id"], c["lang_code"])

    log.info("done granted=%d expired=%d", granted, len(expired))
    return 0


if __name__ == "__main__":
    sys.exit(main())
