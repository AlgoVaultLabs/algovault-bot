"""Validate an api_key passed via the /start auth_<key> deep-link (BOT-W2 C2).

Calls signal-MCP's ``GET /api/bot/validate-key?api_key=<key>`` over the
loopback (same Hetzner host, no Caddy round-trip), authenticated by the
W1 internal-bypass header. Returns the parsed `{valid, customer_id, tier}`
or ``None`` on any non-2xx / shape mismatch / network error.

CRITICAL — never log the api_key value. The /start handler should not log
the deep-link parameter at INFO either; only structured fields like
``{event, has_param, param_kind}``. Same shape as the BOT-W1 httpx
token-leak finding.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final

import httpx


log = logging.getLogger(__name__)

# Loopback default; can be overridden by ALGOVAULT_VALIDATE_KEY_URL for tests.
DEFAULT_VALIDATE_KEY_URL: Final = "http://127.0.0.1:3000/api/bot/validate-key"
HTTP_TIMEOUT: Final = 10.0


@dataclass(frozen=True)
class ValidatedKey:
    customer_id: str | None
    tier: str  # one of: starter | pro | enterprise


def validate_api_key(api_key: str) -> ValidatedKey | None:
    """Round-trip the api_key against signal-MCP. Returns None on any failure."""
    if not api_key or len(api_key) < 8:
        return None

    bypass_key = os.environ.get("ALGOVAULT_INTERNAL_BYPASS_KEY", "").strip()
    if not bypass_key or bypass_key == "__C3_PLACEHOLDER__":
        log.error("validate_api_key: ALGOVAULT_INTERNAL_BYPASS_KEY not configured")
        return None

    url = os.environ.get("ALGOVAULT_VALIDATE_KEY_URL", DEFAULT_VALIDATE_KEY_URL)
    headers = {"X-AlgoVault-Internal-Key": bypass_key}

    try:
        resp = httpx.get(
            url, params={"api_key": api_key}, headers=headers, timeout=HTTP_TIMEOUT
        )
    except httpx.HTTPError as e:
        log.warning("validate_api_key: transport error %s", type(e).__name__)
        return None

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            log.warning("validate_api_key: 200 with non-JSON body")
            return None
        if not data.get("valid") or not data.get("tier"):
            return None
        return ValidatedKey(
            customer_id=data.get("customer_id") or None,
            tier=str(data["tier"]),
        )

    if resp.status_code == 404:
        return None  # api_key not found / not associated with active subscription

    # 401 / 403 / 5xx — log structurally; never log the api_key value.
    log.warning(
        "validate_api_key: HTTP %s (likely env/auth issue)", resp.status_code
    )
    return None
