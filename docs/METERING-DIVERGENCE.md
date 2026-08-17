# The bot meters DELIVERED ALERTS. The API meters VERDICTS. That is deliberate.

**Declared:** 2026-08-09 · **Architect:** Mr.1, ruling G-C of `PRICING-FOLLOWUPS-GENERATOR-W1`
**API-side SoT:** `crypto-quant-signal-mcp` → `src/lib/plans.ts`
**Status:** deliberate divergence for the FREE lane. The PAID lane was unified on 2026-08-17 — see the amendment directly below, which scopes everything after it.

## The question that produced this

`PRICING-FLAT-CALL-BILLING-AND-6MONTH-W1` (deployed 2026-08-09) changed the API's metering rule: **every successful verdict is one metered call, HOLD included** (ruling R-A), on both the subscription meter and the x402 rail. It also added a **second meter** — a per-UTC-day cap enforced independently of the monthly one (R-B) — and moved the free tier to 200/month + 100/day.

This bot enforces its own per-user quota in its own SQLite (`FREE_TIER_MONTHLY_QUOTA` in `src/algovault_bot/quota.py`), and it did **not** change. Two systems now bill what looks like the same action differently, which is exactly the divergence CLAUDE.md's single-derivation rule exists to prevent.

It was examined and **kept**. What follows is why, so nobody "fixes" it.

## Why per-verdict billing has no referent here

The API's billable unit is a **verdict returned to a caller**. Every `get_trade_call` returns something — BUY, SELL, or HOLD — and R-A's whole argument is that a HOLD is a real answer the engine computed and the venues were queried for. The caller receives it. It costs the same to produce. So it counts.

The bot's billable unit is a **delivered alert**. A HOLD is **silent by design** — it produces no message, no notification, nothing the user sees. There is no delivery, so there is nothing to meter. Applying per-verdict billing here would charge a user for alerts that were never sent, which is not a stricter version of the same rule; it is a different and worse rule.

That is the whole divergence, and it follows from the products differing, not from the bot lagging.

## What this means in practice

| | signal-MCP API | this bot |
|---|---|---|
| billable unit | a returned verdict | a **delivered alert** |
| HOLD | counts (R-A) | silent, so nothing to count |
| meters | monthly **and** per-UTC-day, refused independently | monthly only |
| free allowance | 200/month + 100/day | 100 delivered alerts/month |
| enforcement | `plans.ts` → `license.ts` | `quota.py`, this repo's SQLite |

The two numbers are both "100" in places and that is a coincidence of history, not a shared source. **Do not wire one to the other.**

## AMENDED 2026-08-17 — the PAID lane is now UNIFIED. This document is scoped to the FREE lane.

`PRICING-BOT-DELIVERY-METERING-W1` (architect rulings R-1/R-2/R-3) changed what a **paid-linked**
delivery costs. Everything below this section was written when BOTH lanes diverged; it is now true
of the **free** lane only. Nothing below is deleted, because the free lane is still the majority of
this bot's traffic and the reasoning still governs it.

**What changed.** A delivery to a subscriber whose Telegram chat is linked to a paid API key now
DEBITS that subscriber's plan allowance on signal-MCP, through the channel-agnostic
`consumeEntitlement` primitive, and the subscriber is **hard walled** at the plan ceiling. The bot
enqueues the debit locally and a drainer sends it out of band, so a delivery is never blocked by a
metering call.

**What did NOT change, and must not be "tidied" into the new model:**

| | free lane | paid-linked lane |
|---|---|---|
| billable unit | a **delivered alert** | a **delivered alert** — unchanged |
| HOLD | silent, so nothing to meter | silent, so nothing to meter — **Rule 3 below is untouched** |
| ledger | this bot's own SQLite, 100 alerts / rolling 30d | signal-MCP `quota_usage`, via `entitlement_debits` |
| wall | 100 alerts, one notice per window | the plan ceiling, one notice per episode (monthly OR daily) |

**The billable unit is STILL a delivered alert, not a poll.** That is the load-bearing half of this
document and the paid lane inherits it rather than replacing it: measured 2026-08-16, the bot makes
~4,038 MCP requests to deliver ~211 alerts, and polls are unattributable per subscriber by
construction because one scanwatch call serves every subscriber watching that group. Charging polls
was considered and rejected for exactly that reason.

**The parity question is now real for one lane and still absent for the other.** The line below
saying *"there is no parity test … deliberately, because there is no invariant to hold"* remains
correct for the free lane. For the paid lane there IS an invariant — a delivered paid alert must
produce exactly one `entitlement_debits` row — and it is held by the idempotency key
`bot:<chat_id>:<alerts_fired.id>`, not by a test comparing two numbers.

---

## Rules that follow

1. **Bot-facing copy states the bot's units.** "Your free 100 alerts/month" — never "100 calls/month", which reads as the API's meter and is now a different number (200). Any figure quoted from the API ladder must name it as the API's.

   > **This rule is now GATE-ENFORCED, because it never once held as prose.** `BOT-QUOTA-REFUSAL-SEAM-W1` (2026-08-16) found it violated in **nine** live surfaces on the day it was audited — including the footer of every trade-call card image — i.e. it was already false when it was written here. `scripts/check-quota-refusal-seam.py` leg **L3** now bans the collision across every module in the package, matching only real string literals (docstrings and comments excluded via AST, so prose *about* the rule is not judged *by* it). "API calls" stays legal; "free calls" does not. Per the Completeness Standard: a rule that has once failed as prose must be retired into a gate or deleted. Note the shape of the near-miss — L3's first cut scanned a hand-listed three files and reported PASS over the six surfaces it never looked at.
2. **A change to `plans.ts` does not imply a change here.** There is no parity test between the two, deliberately, because there is no invariant to hold.
3. **If this bot ever delivers a HOLD** — a "nothing to do" digest, a silence-confirmation — that is the moment to revisit, because a delivered HOLD *is* a delivery. Reopen this document then.

## What was NOT done, and why

No metering code changed. Ruling G-C scoped this chapter to the declaration alone: an undeclared divergence is the defect, not the divergence itself. Changing behaviour to match a ladder that does not apply would have been the actual mistake.
