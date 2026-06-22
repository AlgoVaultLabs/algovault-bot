"""TG-REFERRAL-W1 / C2 — client for signal-MCP's internal referral API.

The bot resolves/mints a TG user's referral code and records ref-join
attributions by calling crypto-quant-signal-mcp's loopback JSON API
(``GET /api/referral/code`` + ``POST /api/referral/attribute``), authenticated
with the shared internal-bypass key (``ALGOVAULT_INTERNAL_BYPASS_KEY``, sent as
``X-AlgoVault-Internal-Key``). The engine stays the single SoT — the bot never
owns referral codes/attributions, only the bot-side bonus-call pool (db.py).

Sync httpx (mirrors capabilities.py + the bot's existing sync MCP calls in
command handlers). FAIL-SOFT: any error returns None so a transient engine blip
never breaks /start onboarding or /referral — the caller shows a graceful note.
NEVER logs the internal key or the URL with it (httpx INFO leaks URLs — silenced
at the bot's logging setup; we also pass the key only via the header).
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 8.0


def _referral_base() -> str:
    """Derive the API base from ALGOVAULT_MCP_URL (the /api/* sibling of /mcp)."""
    mcp_url = os.environ.get("ALGOVAULT_MCP_URL", "http://127.0.0.1:3000/mcp").rstrip("/")
    return mcp_url[: -len("/mcp")] if mcp_url.endswith("/mcp") else mcp_url


def _headers() -> dict[str, str] | None:
    key = os.environ.get("ALGOVAULT_INTERNAL_BYPASS_KEY", "").strip()
    if not key:
        log.warning('{"event": "referral_client_no_internal_key"}')
        return None
    return {"X-AlgoVault-Internal-Key": key, "content-type": "application/json"}


def get_code(chat_id: int) -> dict[str, Any] | None:
    """Resolve-or-mint the caller's referral code + links + terms + stats.

    Returns the engine payload ``{code, share_url, deep_link, terms{...}, stats{...}}``
    or None on any failure (fail-soft).
    """
    headers = _headers()
    if headers is None:
        return None
    try:
        resp = httpx.get(
            f"{_referral_base()}/api/referral/code",
            params={"tg": str(chat_id)},
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("ok") and isinstance(data.get("code"), str):
            return data
        log.warning('{"event": "referral_get_code_bad_shape"}')
    except (httpx.HTTPError, ValueError) as e:
        log.warning('{"event": "referral_get_code_failed", "err": "%s"}', str(e)[:160])
    return None


def attribute(ref_code: str, chat_id: int) -> dict[str, Any] | None:
    """Record a TG ref-join (referrer code → this referee chat_id).

    Returns ``{recorded: bool, bonus_calls: int, reason?: str}`` or None on
    failure. The engine enforces one-grant-per-tg + self-referral refusal; the
    bonus amount is the SoT value to grant bot-side.
    """
    headers = _headers()
    if headers is None:
        return None
    try:
        resp = httpx.post(
            f"{_referral_base()}/api/referral/attribute",
            json={"ref_code": ref_code, "tg_chat_id": str(chat_id)},
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("ok"):
            return data
        log.warning('{"event": "referral_attribute_bad_shape"}')
    except (httpx.HTTPError, ValueError) as e:
        log.warning('{"event": "referral_attribute_failed", "err": "%s"}', str(e)[:160])
    return None


# ── REFERRAL-PARITY-NOTIFS-W1 / C2: notification queue (drain + opt-out) ──

def get_notifications(limit: int = 100) -> list[dict[str, Any]]:
    """Pull pending TG-channel notifications (the bot drains them). Each row is
    ``{id, code, event, payload}`` (keyed by CODE — the bot maps code→chat_id
    locally). Returns [] on any failure (fail-soft)."""
    headers = _headers()
    if headers is None:
        return []
    try:
        resp = httpx.get(
            f"{_referral_base()}/api/referral/notifications",
            params={"status": "pending", "channel": "tg", "limit": str(limit)},
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("ok") and isinstance(data.get("notifications"), list):
            return data["notifications"]
        log.warning('{"event": "referral_get_notifications_bad_shape"}')
    except (httpx.HTTPError, ValueError) as e:
        log.warning('{"event": "referral_get_notifications_failed", "err": "%s"}', str(e)[:160])
    return []


def mark_delivered(notif_id: int) -> bool:
    """Mark a notification row delivered after a successful Bot-API send. Fail-soft."""
    headers = _headers()
    if headers is None:
        return False
    try:
        resp = httpx.post(
            f"{_referral_base()}/api/referral/notifications/{int(notif_id)}/delivered",
            json={},
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return bool(isinstance(data, dict) and data.get("ok"))
    except (httpx.HTTPError, ValueError) as e:
        log.warning('{"event": "referral_mark_delivered_failed", "err": "%s"}', str(e)[:160])
    return False


def set_notify_pref(chat_id: int, opt_out: bool) -> bool:
    """Write the referrer's notify opt-out pref (single-derivation: same engine
    column as the email unsubscribe link). Fail-soft."""
    headers = _headers()
    if headers is None:
        return False
    try:
        resp = httpx.post(
            f"{_referral_base()}/api/referral/notify-pref",
            json={"tg": str(chat_id), "opt_out": bool(opt_out)},
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return bool(isinstance(data, dict) and data.get("ok"))
    except (httpx.HTTPError, ValueError) as e:
        log.warning('{"event": "referral_set_notify_pref_failed", "err": "%s"}', str(e)[:160])
    return False
