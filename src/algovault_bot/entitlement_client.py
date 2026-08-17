"""PRICING-BOT-DELIVERY-METERING-W1 CH4e — client for signal-MCP's entitlement API.

The bot debits a paid-linked subscriber's PLAN allowance by POSTing to signal-MCP's loopback
``/api/entitlement/consume``, authenticated with the shared internal-bypass key
(``ALGOVAULT_INTERNAL_BYPASS_KEY``, sent as ``X-AlgoVault-Internal-Key``).

🛑 THE BOT NEVER AUTHENTICATES AS THE SUBSCRIBER. It holds ``linked_api_key`` for identity, and
passes it in the BODY as the meter to charge — not as a credential. The request is authorised by
the bot's own internal key. Promoting the subscriber's key to an auth header would make every bot
delivery indistinguishable from the customer's own traffic and would expose bot alerts to their
rate limits.

Shaped on ``link_validator.py`` / ``referral_client.py``: sync httpx, base URL derived from
``ALGOVAULT_MCP_URL`` via ``referral_client._referral_base()`` (NO third URL env var), and
FAIL-SOFT — any transport error or non-200 returns ``None`` so a transient engine blip leaves the
outbox row pending for the next drain rather than dropping a debit or raising into the drainer.

NEVER logs the internal key, the subscriber's key, or a URL carrying either (httpx INFO leaks
URLs — silenced at the bot's logging setup; the keys travel in a header and a JSON body).
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .referral_client import _referral_base

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10.0


def _headers() -> dict[str, str] | None:
    key = os.environ.get("ALGOVAULT_INTERNAL_BYPASS_KEY", "").strip()
    if not key:
        log.warning('{"event": "entitlement_client_no_internal_key"}')
        return None
    return {"X-AlgoVault-Internal-Key": key, "content-type": "application/json"}


def consume(
    api_key: str,
    channel: str,
    units: int,
    idempotency_key: str,
    kind: str,
) -> dict[str, Any] | None:
    """Debit ``units`` against ``api_key``'s plan. Returns the decision dict, or None.

    ``idempotency_key`` is REQUIRED and is never minted here — it is ``bot:<chat>:<alerts_fired.id>``,
    supplied by the caller from the delivery ledger. A client-minted key would differ on every
    retry, which is precisely the case the guard exists for.

    A 200 with ``outcome: REFUSED`` is a SUCCESSFUL call reporting a business decision, not a
    failure — the caller stamps the row and arms the wall. Only transport faults and non-200s
    return None.
    """
    h = _headers()
    if h is None:
        return None
    try:
        r = httpx.post(
            f"{_referral_base()}/api/entitlement/consume",
            headers=h,
            json={
                "api_key": api_key,
                "channel": channel,
                "units": units,
                "idempotency_key": idempotency_key,
                "kind": kind,
            },
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            # 404 (unknown/expired key) is terminal for this row, but classifying it is the
            # drainer's job — the client reports the status and stays dumb.
            log.warning(
                '{"event": "entitlement_consume_non_200", "status": %d, "idem": "%s"}',
                r.status_code,
                idempotency_key,
            )
            return {"_http_status": r.status_code}
        return dict(r.json())
    except Exception as err:  # noqa: BLE001 — fail soft, never raise into the drainer
        log.warning(
            '{"event": "entitlement_consume_failed", "idem": "%s", "err": "%s"}',
            idempotency_key,
            str(err)[:200],
        )
        return None


def read_state(api_key: str, channel: str = "bot") -> dict[str, Any] | None:
    """Read plan state WITHOUT charging — the mirror-refresh poll for an idle subscriber.

    This is what keeps a paying subscriber's mirror warm when they take no alerts, and what
    RE-OPENS a wall after the server's period resets. Without it a walled subscriber would stay
    walled locally until their next delivery, which is the one thing the wall prevents.
    """
    h = _headers()
    if h is None:
        return None
    try:
        r = httpx.get(
            f"{_referral_base()}/api/entitlement/state",
            headers=h,
            params={"api_key": api_key, "channel": channel},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return {"_http_status": r.status_code}
        return dict(r.json())
    except Exception as err:  # noqa: BLE001
        log.warning('{"event": "entitlement_state_failed", "err": "%s"}', str(err)[:200])
        return None
