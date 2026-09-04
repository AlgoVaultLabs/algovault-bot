"""Validate an api_key passed via the /start auth_<key> deep-link (BOT-W2 C2).

Calls signal-MCP's ``GET /api/bot/validate-key?api_key=<key>`` over the
loopback (same Hetzner host, no Caddy round-trip), authenticated by the
W1 internal-bypass header.

OPS-BOT-LINKED-TIER-REFRESH-W1 CH1 — THREE STATES, NOT TWO.

This module used to return ``ValidatedKey | None`` and collapse six distinct
conditions into that one ``None``: a malformed key, OUR bypass key being unset,
a transport error, a non-JSON body, a determined "no active subscription", and
a 401/403/5xx. ``handle_link`` then mapped every one of them to "your key is
invalid" — so a misconfigured bypass key, or a signal-MCP blip, told a PAYING
customer their key was bad, on the paid conversion funnel.

That is the estate's third sentinel-collapse, after ``fetch 000`` in
``declaration-sync.sh`` and ``code=000``, and the fix is the same one those
took: carry the distinguishing detail BESIDE the verdict so a future gap is
visible rather than absorbed. Hence ``KeyCheck.reason``, populated on EVERY
outcome including ``VALID``.

THE RULE THIS MODULE EXISTS TO ENFORCE: an INDETERMINATE is not a fact about
the subscriber's key. It is a fact about US. Nothing downstream may refuse,
downgrade or unlink on one.

KNOWN AMBIGUITY UPSTREAM, MEASURED 2026-08-21 — ``404`` IS NOT PURELY
DETERMINED. signal-MCP's ``validateApiKey`` already distinguishes indeterminacy
(it returns ``{valid:false, indeterminate:true}`` when Stripe is unconfigured or
unreachable, and calls ``recordIndeterminate('stripe_validate_api_key')``) — but
its HTTP route drops that flag and emits a bare ``404 {"valid":false}`` for BOTH
"no active subscription" and "we could not ask Stripe". From out here the two
are indistinguishable, so ``404`` is classified ``INVALID`` below. That is safe
for ``/link`` (a wrong retry costs one click) and is NOT safe on its own for a
downgrade, which is why the revalidation loop additionally requires COHORT
CORROBORATION before advancing any invalid streak: if every linked key 404s at
once that is an outage, not N simultaneous cancellations. Retiring the ambiguity
at its source is ``OPS-VALIDATE-KEY-INDETERMINATE-W{NEXT}``.

CRITICAL — never log the api_key value. The /start handler should not log the
deep-link parameter at INFO either; only structured fields like
``{event, has_param, param_kind}``. Same shape as the BOT-W1 httpx token-leak
finding. ``reason`` is drawn from a fixed vocabulary plus an exception CLASS
NAME or an HTTP status — never from caller input.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final, Literal

import httpx


log = logging.getLogger(__name__)

# Loopback default; can be overridden by ALGOVAULT_VALIDATE_KEY_URL for tests.
DEFAULT_VALIDATE_KEY_URL: Final = "http://127.0.0.1:3000/api/bot/validate-key"
HTTP_TIMEOUT: Final = 10.0
#: Shorter than this cannot be one of ours — refused before any network call.
MIN_KEY_LEN: Final = 8
#: The C3 scaffold value. Present-but-placeholder is misconfigured, same as unset.
PLACEHOLDER_BYPASS_KEY: Final = "__C3_PLACEHOLDER__"

KeyStatus = Literal["VALID", "DUNNING", "INVALID", "INDETERMINATE"]


@dataclass(frozen=True)
class KeyCheck:
    """The outcome of one validate-key round-trip.

    ``tier`` and ``customer_id`` are set IFF ``status == "VALID"`` — enforced by
    construction, since the only way to build a VALID is ``_valid()``.
    """

    status: KeyStatus
    tier: str | None
    customer_id: str | None
    reason: str

    @property
    def is_determined_invalid(self) -> bool:
        """True only for a *determined* negative.

        Every path that could reduce a subscriber's entitlement gates on THIS,
        never on ``not is_valid`` — that is the whole distinction the class
        exists to carry, and reading it at the call site is what makes the rule
        checkable rather than remembered.
        """
        return self.status == "INVALID"

    @property
    def is_valid(self) -> bool:
        return self.status == "VALID"

    @property
    def is_dunning(self) -> bool:
        """A determined POSITIVE that is not an entitlement grant. See `_dunning`."""
        return self.status == "DUNNING"

    @property
    def corroborates(self) -> bool:
        """May this answer serve as proof the validator is up RIGHT NOW?

        VALID or DUNNING only — both are POSITIVE determinations that required Stripe to answer.
        An INVALID deliberately does NOT corroborate: before
        OPS-VALIDATE-KEY-INDETERMINATE-W1 CH2 a bare 404 could be an outage, and this bot may be
        running against a server that predates it. Keeping corroboration keyed on a positive
        answer means the fail-safe holds under EITHER server version, which is what makes CH3
        safe to deploy in any order.
        """
        return self.status in ("VALID", "DUNNING")


def _valid(tier: str, customer_id: str | None) -> KeyCheck:
    return KeyCheck(status="VALID", tier=tier, customer_id=customer_id, reason="ok")


def _dunning(tier: str | None, customer_id: str | None, reason: str) -> KeyCheck:
    """Stripe is still COLLECTING from this customer. OPS-VALIDATE-KEY-INDETERMINATE-W1 CH3.

    Not VALID (they are not entitled to API access) and emphatically not INVALID (they have a
    subscription and an open invoice Stripe is retrying). Folding it into either is what this
    wave exists to stop: as INVALID it advanced a downgrade streak against a paying customer,
    and it made every delivery free because the debit 404'd.
    """
    return KeyCheck(status="DUNNING", tier=tier, customer_id=customer_id, reason=reason)


def _invalid(reason: str) -> KeyCheck:
    return KeyCheck(status="INVALID", tier=None, customer_id=None, reason=reason)


def _indeterminate(reason: str) -> KeyCheck:
    return KeyCheck(status="INDETERMINATE", tier=None, customer_id=None, reason=reason)


def validate_api_key(api_key: str) -> KeyCheck:
    """Round-trip the api_key against signal-MCP. ALWAYS returns a KeyCheck.

    Never returns None, and never raises: a caller must be able to tell "no"
    from "don't know" without a try/except, because the two demand opposite
    actions.
    """
    if not api_key or len(api_key) < MIN_KEY_LEN:
        return _invalid("malformed_key")

    bypass_key = os.environ.get("ALGOVAULT_INTERNAL_BYPASS_KEY", "").strip()
    if not bypass_key or bypass_key == PLACEHOLDER_BYPASS_KEY:
        # OUR env being wrong is not a fact about THEIR key. This single row is the live
        # conversion bug the wave was raised for: it used to read as INVALID.
        log.error("validate_api_key: ALGOVAULT_INTERNAL_BYPASS_KEY not configured")
        return _indeterminate("bot_misconfigured")

    url = os.environ.get("ALGOVAULT_VALIDATE_KEY_URL", DEFAULT_VALIDATE_KEY_URL)
    headers = {"X-AlgoVault-Internal-Key": bypass_key}

    try:
        resp = httpx.get(
            url, params={"api_key": api_key}, headers=headers, timeout=HTTP_TIMEOUT
        )
    except httpx.HTTPError as e:
        log.warning("validate_api_key: transport error %s", type(e).__name__)
        return _indeterminate(f"transport_{type(e).__name__}")

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            log.warning("validate_api_key: 200 with non-JSON body")
            return _indeterminate("bad_body")
        if not isinstance(data, dict):
            log.warning("validate_api_key: 200 with non-object JSON body")
            return _indeterminate("bad_body")
        # OPS-VALIDATE-KEY-INDETERMINATE-W1 CH3 — read the state the server now states outright.
        # Absent on a server predating CH2, in which case the legacy `valid` branch below still
        # governs and behaviour is byte-identical to before this wave.
        state = data.get("entitlement_state")
        if state == "DUNNING":
            dun = data.get("dunning") or {}
            return _dunning(
                tier=str(dun.get("tier")) if dun.get("tier") else None,
                customer_id=data.get("customer_id") or None,
                reason="past_due",
            )
        if state == "INDETERMINATE":
            # Belt and braces: this arrives as a 503 and is handled below. A 200 carrying it
            # would be the server contradicting itself, and the safe reading is still "unknown".
            return _indeterminate("server_indeterminate")
        if not data.get("valid"):
            # Defensive: the live endpoint answers 404 for this, never 200 (measured
            # 2026-08-21). Kept because a shape we stopped expecting is exactly the kind
            # that comes back, and the branch is free.
            return _invalid(str(state) if state else "no_active_subscription")
        tier = data.get("tier")
        if not tier:
            # 200 asserting valid=true but carrying NO tier is a SHAPE MISMATCH — the
            # server contradicting itself — not a determination about the subscriber.
            # The pre-CH1 code folded it into the same None as a real rejection.
            log.warning("validate_api_key: 200 valid=true with no tier")
            return _indeterminate("bad_body")
        return _valid(tier=str(tier), customer_id=data.get("customer_id") or None)

    if resp.status_code == 404:
        # Determined: no subscription, or one that has ENDED. Since
        # OPS-VALIDATE-KEY-INDETERMINATE-W1 CH2 this is unambiguous — a Stripe outage answers
        # 503 and a dunning customer answers 200, so 404 finally means what it says.
        reason = "no_active_subscription"
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("reason"):
                reason = str(body["reason"])[:64]
        except ValueError:
            pass
        return _invalid(reason)

    # 401 / 403 / 5xx — our auth is broken or the server is down. Both are OURS.
    # Log structurally; never log the api_key value.
    log.warning("validate_api_key: HTTP %s (likely env/auth issue)", resp.status_code)
    return _indeterminate(f"http_{resp.status_code}")
