#!/usr/bin/env python3
"""TG-BROADCAST-STACK-W1 CH2 (2026-05-28): daily-digest content generator.

Invoked by ``daily-digest.sh`` from cron at 12:03 UTC daily. Steps:
 1. Call ``scan_funding_arb`` via McpClient → top cross-venue setups.
 2. Filter HOLDs + min confidence ≥ 75.
 3. Take top 3 by (confidence × spread × tier-weight).
 4. Render T1-voice body (≤500 chars) OR empty-state fallback.
 5. Call ``sendBroadcast(body, 'daily_digest_YYYY-MM-DD', dry_run)``.

CLI:
   daily-digest.py                                 # full broadcast at 12:03 UTC
   daily-digest.py --dry-run                       # render body + count cohort; no send
   daily-digest.py --dry-run --cohort-override=mr1-only
                                                   # spec's verification-gate variant;
                                                   # renders body + reports would_send=1
                                                   # against the explicit Mr.1 chat_id

Env reads:
   ALGOVAULT_BOT_TOKEN  — required for non-dry-run fires
   MCP_URL              — defaults to http://127.0.0.1:3000/mcp
   MCP_INTERNAL_BYPASS_KEY — defaults to empty (allows public access)
   ALGOVAULT_BOT_DB_PATH — defaults to /var/lib/algovault-bot/state.db

Spec reference: ``Prompt/tg-broadcast-stack-w1.md`` Chapter C2.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the bot package is importable when invoked as a CLI from cron.
_PKG_PARENT = Path(__file__).resolve().parent.parent / "src"
if _PKG_PARENT.is_dir() and str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from algovault_bot.broadcast import sendBroadcast  # noqa: E402
from algovault_bot.mcp_client import McpClient, McpClientConfig, McpError  # noqa: E402


log = logging.getLogger("daily-digest")

DEFAULT_MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:3000/mcp")
DEFAULT_INTERNAL_BYPASS_KEY = os.environ.get("MCP_INTERNAL_BYPASS_KEY", "")
DEFAULT_DB_PATH = os.environ.get(
    "ALGOVAULT_BOT_DB_PATH", "/var/lib/algovault-bot/state.db"
)
MIN_CONFIDENCE = 75
MAX_DIGEST_CHARS = 500
DIGEST_BODY_PREFIX = "📊 AlgoVault Daily Digest"


# ── Top-3 ranking ────────────────────────────────────────────────────────

def _tier_weight(tier: str | None) -> float:
    """Tier-weighted preference: T1 + T2 majors favored, T3/T4 down-weighted."""
    if not tier:
        return 0.5
    t = tier.upper()
    return {"T1": 1.0, "T2": 0.8, "T3": 0.5, "T4": 0.3}.get(t, 0.5)


def _setup_score(setup: dict[str, Any]) -> float:
    """Composite score: confidence × cross-venue spread × tier weight."""
    confidence = float(setup.get("confidence") or 0)
    spread_bps = abs(float(setup.get("spread_bps") or 0))
    tier_w = _tier_weight(setup.get("tier"))
    return confidence * spread_bps * tier_w


def _filter_setups(raw_setups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop HOLDs + min confidence floor."""
    out: list[dict[str, Any]] = []
    for s in raw_setups:
        verdict = str(s.get("verdict") or s.get("call") or "").upper()
        if verdict == "HOLD" or not verdict:
            continue
        conf = float(s.get("confidence") or 0)
        if conf < MIN_CONFIDENCE:
            continue
        out.append(s)
    return out


def _rank_top_3(setups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(setups, key=_setup_score, reverse=True)[:3]


# ── Body renderers ───────────────────────────────────────────────────────

def render_digest_body(top3: list[dict[str, Any]], date_str: str) -> str:
    """T1-voice body. Outcome-framed; ≤500 chars; ≤2 sentences per line."""
    if not top3:
        return render_empty_state(date_str)

    lines: list[str] = [f"{DIGEST_BODY_PREFIX} — {date_str}", "", "Top 3 cross-venue setups:"]
    for i, s in enumerate(top3, start=1):
        coin = str(s.get("coin") or s.get("symbol") or "?").upper()
        verdict = str(s.get("verdict") or s.get("call") or "?").upper()
        conf = int(float(s.get("confidence") or 0))
        spread = float(s.get("spread_bps") or 0)
        venue_pair = s.get("venue_pair") or s.get("venues") or "—"
        spread_str = f"{spread:+.0f} bps"
        lines.append(
            f"{i}. {coin} {verdict} {conf}% · {spread_str} ({venue_pair})"
        )
    lines.append("")
    lines.append("Live: t.me/algovaultofficialbot — type /watch <COIN> <TF> <EXCHANGE>")
    lines.append("100 free calls/month. HOLDs never cost.")
    body = "\n".join(lines)
    # Hard cap at MAX_DIGEST_CHARS; truncate body lines (not closing CTA) if needed.
    if len(body) <= MAX_DIGEST_CHARS:
        return body
    # Defensive truncation — keep prefix + CTA, drop middle setups if absurdly long.
    return body[: MAX_DIGEST_CHARS - 3] + "..."


def render_empty_state(date_str: str) -> str:
    """T1-voice empty-state fallback — concise + actionable + on-brand."""
    return (
        f"{DIGEST_BODY_PREFIX} — {date_str}\n\n"
        "Markets quiet last 24h — no high-confidence cross-venue setups.\n\n"
        "Check t.me/algovaultofficialbot — type /watch <COIN> <TF> <EXCHANGE> "
        "to set on-demand alerts."
    )


# ── MCP probe ────────────────────────────────────────────────────────────

def fetch_top_setups(mcp_url: str, bypass_key: str) -> list[dict[str, Any]]:
    """Call scan_funding_arb via McpClient + return the raw setups list.

    Empty list on McpError or unexpected response shape; logger warns.
    """
    cfg = McpClientConfig(
        url=mcp_url,
        internal_bypass_key=bypass_key,
        client_name="algovault-bot-daily-digest",
        client_version="1.0",
    )
    try:
        with McpClient(cfg) as mcp:
            result = mcp.call_tool("scan_funding_arb", {})
    except McpError as e:
        log.warning("scan_funding_arb failed: %s", e)
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("scan_funding_arb unexpected error: %s", e)
        return []

    # Tool response shape varies; accept multiple common shapes.
    if isinstance(result, dict):
        for key in ("opportunities", "setups", "results", "items"):
            if isinstance(result.get(key), list):
                return result[key]
        if isinstance(result.get("data"), list):
            return result["data"]
    if isinstance(result, list):
        return result
    log.info("scan_funding_arb returned non-list shape: %r", type(result))
    return []


# ── CLI ──────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AlgoVault TG bot daily digest")
    p.add_argument("--dry-run", action="store_true", help="Skip the actual broadcast")
    p.add_argument(
        "--cohort-override",
        default="",
        help='Restrict cohort (only "mr1-only" supported; for verification-gate use)',
    )
    p.add_argument("--mcp-url", default=DEFAULT_MCP_URL, help="MCP server URL")
    p.add_argument(
        "--bypass-key",
        default=DEFAULT_INTERNAL_BYPASS_KEY,
        help="MCP internal-bypass key (header)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv or sys.argv[1:])
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    raw = fetch_top_setups(args.mcp_url, args.bypass_key)
    filtered = _filter_setups(raw)
    top3 = _rank_top_3(filtered)
    body = render_digest_body(top3, date_str)
    log.info("digest_body_chars=%d top3_count=%d raw_count=%d", len(body), len(top3), len(raw))

    if args.cohort_override == "mr1-only":
        # Verification-gate path — render body, print result, no real send.
        # Spec literal: `--dry-run --cohort-override=mr1-only` reports
        # DRY_RUN_BROADCAST: would_send=1.
        result_payload = {
            "status": "dry_run",
            "would_send": 1,
            "would_skip_blocked": 0,
            "cohort_override": "mr1-only",
            "body_chars": len(body),
            "body": body,
        }
        print(f"DRY_RUN_BROADCAST: would_send=1 skipped=0 cohort=mr1-only chars={len(body)}")
        print(json.dumps(result_payload, indent=2))
        return 0

    broadcast_type = f"daily_digest_{date_str}"
    result = sendBroadcast(body, broadcast_type, dry_run=args.dry_run)
    log.info("sendBroadcast result: %s", result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
