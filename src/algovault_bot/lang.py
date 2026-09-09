"""OPS-BOT-DEAD-SURFACE-SWEEP-W1 CH1 (2026-09-09): the trilingual routing leaf.

``normalize_lang`` moved here BYTE-IDENTICAL from ``unlock.py:72-81``. It was the one
live symbol trapped inside a module about to be deleted: ``referral.py`` imported it
across 8 call sites, so deleting ``unlock.py`` with the symbol still inside would have
stopped the package importing and taken every cron down with it. The move is therefore
the ordering constraint the whole wave rests on, not a tidy-up — CH2's deletions are
only safe once this file exists and is green.

WHY THIS IS A LEAF, AND WHY IT MUST STAY ONE
────────────────────────────────────────────
This module imports NOTHING local. That is the property ``referral.py:3`` depends on and
the reason it can be imported from anywhere without a cycle: ``referral.py`` is itself
declared PURE (no telegram/httpx/db), and it would stop being pure the moment its language
router reached back into a module with framework imports.

So this file holds ``normalize_lang`` and nothing else. It has no constants of its own —
``PENDING_NPM_EXPIRY_HOURS``, ``CB_UNLOCK_*`` and ``GRANT_DURATION_DAYS`` all belonged to
the unlock mechanic and died with it. Do not give this module a second responsibility;
a leaf that grows one stops being importable from everywhere, which is the only thing it
is for.

Consumers: ``referral.py`` (8 call sites).
"""
from __future__ import annotations


def normalize_lang(lang_code: str | None) -> str:
    """Trilingual routing: returns 'id' / 'zh-hans' / 'en' fallback."""
    if not lang_code:
        return "en"
    lc = lang_code.lower().replace("_", "-")
    if lc.startswith("id"):
        return "id"
    if lc.startswith("zh"):
        return "zh-hans"
    return "en"
