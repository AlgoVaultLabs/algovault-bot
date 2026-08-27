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
2. ~~**A change to `plans.ts` does not imply a change here.** There is no parity test between the two, deliberately, because there is no invariant to hold.~~
   > 🛑 **RETIRED 2026-08-27 (`GROWTH-TG-QUOTA-PARITY-W1`). This rule is now the OPPOSITE of the truth.** A ladder change PROPAGATES: `plans.ts` publishes the free rung at `GET /api/plans/public`, the bot mirrors it on the existing entitlement drain, and `quota.FREE_TIER_MONTHLY_QUOTA` / `FREE_TIER_DAILY_QUOTA` are pinned FALLBACKS for when that mirror is unreadable — not the answer. See the amendment below.
3. **If this bot ever delivers a HOLD** — a "nothing to do" digest, a silence-confirmation — that is the moment to revisit, because a delivered HOLD *is* a delivery. Reopen this document then.

## What was NOT done, and why

No metering code changed. Ruling G-C scoped this chapter to the declaration alone: an undeclared divergence is the defect, not the divergence itself. Changing behaviour to match a ladder that does not apply would have been the actual mistake.


---

## Amendment — 2026-08-27 (`GROWTH-TG-QUOTA-PARITY-W1`)

**Architect ruling (Mr.1):** *"TG bot should have the same cap: 200 calls/month, 100 calls/day, for
free tier."* This **reverses ruling G-C of `PRICING-FOLLOWUPS-GENERATOR-W1` on the ALLOWANCE only.**

### What changed

| | before | after |
|---|---|---|
| free monthly | 100 alerts, hand-typed in `quota.py` | **200**, derived from `plans.ts` via `/api/plans/public` |
| free daily | none | **100 per UTC day** |
| binding | *"Do not wire one to the other"* | the ladder is MIRRORED on the existing entitlement drain |

### What was RETIRED

- *"Do not wire one to the other."* Wired. One SoT (`src/lib/plans.ts`), one public projection, one
  mirror.
- **Rule 2** above — *"a change to `plans.ts` does not imply a change here"*. Struck through in
  place rather than deleted, because the record of what this document once asserted is the point.

### What SURVIVES — and this is the load-bearing half

- **Rule 3 is UNTOUCHED.** The billable unit is still a **delivered alert**, and a HOLD is still
  silent and therefore still free. The API meters a returned verdict, HOLD included. Unifying the
  allowance did not unify the unit, and nothing here suggests it should.
- **Rule 1 SURVIVES AND IS STRENGTHENED.** Bot-facing copy still states the bot's own noun,
  `alerts`, because a HOLD costs the user nothing here and `calls` would overstate what they spend.
  **The two ladders are now the same NUMBER with different UNITS** — which makes the noun matter
  more than it did when the numbers also differed, not less. Leg **L3** still enforces it; new leg
  **L5** additionally makes a hand-typed *allowance* unwritable in any shipped string.

### The daily cap is PARITY + HEADROOM. It is not a tightening.

🛑 **The word "tightening" is banned from this wave's copy, this document included, and so is any
claim that the cap will never bind.** Both would be unevidenced in opposite directions.

What was actually measured (CH0/P5, read-only against production `state.db` on 2026-08-27, over
`alerts_fired` spanning 2026-05-28 → 2026-08-27):

- **298 free chat-days** observed
- **maximum 74 alerts** delivered to any free subscriber in one UTC day
- **mean 4.80** alerts/day
- **zero** free chat-days above 100

The single chat that ever exceeded 100 alerts in a day (`8776880162`, peak 248) is
`linked_tier = starter` — **paid**, and therefore governed by the plan ceiling, not this meter.

So the 100/UTC-day cap sits **26% above the highest daily volume any free subscriber has ever
received**, and no free subscriber in the recorded window would have been refused by it. That is a
statement about the PRE-CHANGE distribution under a 100/month ceiling, and nothing more: doubling
the monthly allowance can move daily peaks, so this is not a promise that the cap will never bind.
**`GROWTH-TG-DAILY-CAP-IMPACT-W1` re-measures against exactly this baseline at ~30 days.**

### Why the daily cap is a CALENDAR day when the monthly window is ROLLING

Deliberate, and it is a copy constraint rather than an implementation preference. The 30-day window
starts at each subscriber's own first alert, so its reset date is a property of when they happened
to arrive — it cannot be stated in advance. `00:00 UTC` can. The daily-wall copy names that clock,
which is the same defect `PRICING-FOLLOWUPS-GENERATOR-W1` CH1 fixed on the API side when production
told a caller walled for two hours to come back in 30 days.
