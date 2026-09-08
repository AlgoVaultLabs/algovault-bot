"""TOMBSTONE — the paywall-at-quota hook, retired in place.

Originally TG-BROADCAST-STACK-W1 CH3 (2026-05-28): fire a one-time DM when signal-MCP returned
`_algovault.tier_warning` on a tool call. GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1
R2c emptied it. The file is kept, rather than deleted, so this record sits where the next reader
looks — and because deleting a module is a wider act than the chapter was scoped for.

WHY EVERYTHING HERE IS GONE
───────────────────────────
The hook never fired. `alert_engine.py`'s own comment recorded it as dark for ~80 days, and the
reason is structural: the bot authenticates with `X-AlgoVault-Internal-Key`, so signal-MCP's
`withTierWarning` returns the meta unchanged for bot-internal callers and the field this module
keyed on is unreachable by construction. `should_fire_paywall_dm`, `has_fired_this_month`,
`mark_fired` and `extract_tier_warning` had ZERO callers outside their own tests — 0 of 57
subscribers were ever stamped.

`format_paywall_body` was the one live symbol, reached from `quota.build_refusal_text` since
BOT-QUOTA-REFUSAL-SEAM-W1. Its two reachable levels (`block`, `daily_block`) are now composed by
`notices.compose_wall`, which owns the body AND the keyboard — the split between them is exactly
how the free wall came to ship a working button beside a scheme-less URL in its own text. Its
three unreachable levels went with it:

  soft / hard              — only `should_fire_paywall_dm` could select them, and nothing called it
  block + referral args    — `build_refusal_text` never passed `referral_link`/`bonus_calls`
  `{url}` interpolation    — the class this whole wave retires
  the "30 days free Pro"   — MEASURED FALSE: the unlock grant writes `tg_pro_grants` +
  clause at :178-:207        `unlock_status` and NOTHING reads either in the metering path, so the
                             claim promised a benefit the code cannot deliver. It survives in
                             `unlock.py`'s frozen strings and is handed to
                             TG-UNLOCK-PRO-GRANT-DARK-W{NEXT} with the measured count: ZERO grants
                             have ever been issued.

WHAT MOVED, AND WHERE TO LOOK FOR IT
────────────────────────────────────
  the trilingual `Resets …` fallback  ->  `quota.reset_sentence("free", …)`, byte-identical
  the <= 300-char discipline          ->  asserted per language in `tests/test_notices.py`
  the `block` / `daily_block` bodies  ->  `notices.compose_wall` (§Copy A / §Copy B)

Deleting the file outright is `OPS-BOT-PAYWALL-MODULE-RETIRE-W{NEXT}` — a one-line change once
`tests/test_paywall_hook.py` and `tests/test_paywall_daily_block.py` have been re-homed onto the
composer, which is a test-layout decision rather than a behaviour one.
"""
from __future__ import annotations
