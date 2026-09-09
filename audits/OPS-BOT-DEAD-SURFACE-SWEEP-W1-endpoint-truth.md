# OPS-BOT-DEAD-SURFACE-SWEEP-W1 — endpoint truth

Plan-Mode Step 0, executed 2026-09-09. Every probe read-only, against `origin/main 475ddd7`
and the live host `signal-1` (204.168.185.24, box UTC at probe `2026-09-09T06:25:34Z`).

**Verdict:** HALT on ≥3 fictional primitives → architect ruling `GO WITH SCOPE CHANGE`, 10/10
answered, 0 PENDING. §Map Anchor REPLACED; Carried Ruling 2, R4, R7 and the `lang.py` Taxonomy
row AMENDED. Chapter count, ordering, gates, the signal-MCP freeze and the no-schema-change
ruling are UNCHANGED.

Baseline at `475ddd7`: **1147 passed, 1 skipped**; `ruff` + `mypy` clean over 41 source files.

---

## P1–P6

| # | Claim | Reality | Resolution |
|---|---|---|---|
| P1 | `normalize_lang` at `unlock.py:72`; `referral.py:15` + 8 sites `:24,:71,:91,:152,:178,:190,:212,:253`; `handlers.py:2007` | Exact — all 11 line numbers confirmed | CH1 proceeds. `handlers.py:2007` needs **no** repoint: it sits inside `_on_photo` (1921‑2016), which CH2 deletes |
| P2 | All importers inside the deletion set except P1's | **Zero importers outside the set.** `paywall` has **zero import statements anywhere in the tree** | R7's precondition confirmed. No HALT on this leg |
| P3 | `check-npm-unlocks.sh` scheduled; in neither registry | Scheduled (count=1); absent from `monitoring-inventory.json` **and** `ops/scripts/cron-interlock-registry.json` | Cron removed in CH2 R5. See F1 for the spec's wrong registry path, and the new finding below |
| P4 | The signal-MCP half is live — confirm and leave alone | `src/lib/track-token.ts` present with `resolveSessionIdentity` + `parseTrackTokenFromArgv` / `captureArgvTrackToken` / `getArgvTrackToken`. **15 files under `landing/` name the header** | Confirmed LIVE. Nothing in `crypto-quant-signal-mcp` is touched. Decomposition recorded below |
| P5 | `/var/lib/algovault-bot/screenshots` empty | Exists, **0 files**, mtime 2026‑05‑29 | Directory stays (Data Integrity). Nothing to delete |
| P6 | Does `check-quota-refusal-seam.py`'s L2b generalise? | **No.** L2b's corpus is the hand-declared `quota.REFUSAL_LANES` dict, not the filesystem — it would not have caught `paywall.py` either, since `format_paywall_body` was never a declared lane | Write the separate AST walker as specced. Reuse `hooks/pre-push`'s seam block shape **including its `|| true`**, which that file documents as load-bearing |

### P4 decomposition (recorded per the numerical-citation rule)

15 files under `landing/` name `X-AlgoVault-Track-Token`:

- **12** under `landing/integrations/` — alpaca · binance-agent-os · claude-code · claude-desktop ·
  cline · codex · cursor · deepseek-harness · gemini · glm-zcode · kimi · kraken
- **3** elsewhere under `landing/` — `docs.html` · `mcp.html` · `llms-full.txt`

The spec's total was right; its parenthetical listed only the 12 integration slugs.

---

## Fictional primitives — 3 (HALT threshold), all resolved by the architect ruling

| # | Spec said | Measured | Resolution |
|---|---|---|---|
| F1 | `ops/cron/cron-interlock-registry.json` | Does not exist | Real path `ops/scripts/cron-interlock-registry.json`; its checker is `scripts/check-cron-interlock-coverage.mjs` at the signal-MCP repo root (vitest twin `tests/unit/cron-interlock-coverage.test.ts`) |
| F2 | `db.py` methods `mark_unlock_*`, `clear_unlock_*` | Do not exist | Real names `set_unlock_expired`, `reset_unlock_state` |
| F3 | §Map Anchor: *remove* the `/unlock_premium_alerts` mechanic, 2 callbacks, photo handler, `tg_pro_grants` path, the `check-npm-unlocks` cron, the `paywall.py` node, and the `funnel_events` poll edge | `system-map.md` has **0 hits** for `unlock_premium` · `paywall` · `tg_pro_grants` · `check-npm-unlocks` · `first_tool_call` · `track_token` · `screenshot`. The `algovault-bot` row (`:1080`) never described the mechanic; `BOT`'s only postgres edge is `bot_daily_metrics` (`:139`) | **Every removal clause struck.** §Map Anchor replaced by Q1's ruling: ADD the `lang.py` leaf and `check-orphan-modules` to the `algovault-bot` row, remove nothing, `system-map.md updated: Y`, `Last touched:` overwritten in place |

Line-number drift in R4/CH2 is **not** counted as fictional — the spec header pre-authorised
re-derivation. Corrected below.

---

## Re-derived line numbers (`db.py` @ 475ddd7, 2322 lines)

| Region | Spec cited | Measured |
|---|---|---|
| `PAYWALL_HOOK_MIGRATIONS` | *(omitted)* | comment `221‑224`, tuple **`225‑229`** |
| `UNLOCK_STATE_MIGRATIONS` | `:533‑540` | comment `532‑535`, tuple **`536‑550`** (ALTERs at `537‑540`; **`referral_code` at `:549` is LIVE**) |
| `PRO_GRANTS_TABLE_MIGRATIONS` | `:548‑559` | comment `552‑555`, tuple **`556‑565`** |
| `NPM_UNLOCK_MIGRATIONS` | `:563‑567` | comment `567‑571`, tuple **`572‑575`** |
| unlock/grant CRUD | `~:1563‑1660` | **`1562‑1660`** — `get_unlock_state` · `set_unlock_pending` · `set_unlock_screenshot_path` · `set_unlock_verified` · `set_unlock_expired` · `reset_unlock_state` · `set_npm_unlock_detected_at` · `get_pro_grant` · `insert_or_replace_pro_grant` |

## The ten dead identifiers (Q3 — supersedes Carried Ruling 2's "four")

Six `subscribers` columns from the unlock mechanic — `unlock_status` · `unlock_verified_at` ·
`unlock_method` · `unlock_screenshot_path` · `npm_unlock_session_id` · `npm_unlock_detected_at` —
plus the table `tg_pro_grants`, plus three from `PAYWALL_HOOK_MIGRATIONS` — `quota_hit_soft_at` ·
`quota_hit_hard_at` · `quota_hit_block_at`.

Re-probed stated absence: **no reader of any of the ten outside `db.py`'s own CRUD and the
`handlers.py` functions CH2 deletes.** The `quota_hit_*` three have zero references anywhere
except their own `ALTER` statements — `has_fired_this_month` and `mark_fired` died with the
composer wave. The wave's motivating premise holds.

## `paywall.py` prose references — 22 across 11 files outside the module

**REWRITE (7)** — each cites `paywall.py` in the PRESENT tense as a live module whose idiom is
being copied, and each dangles the moment the file is gone:
`quota.py:49` · `quota.py:55` · `quota.py:90` · `quota.py:967` · `keyboards.py:41` ·
`messages.py:25` · `plan_ladder.py:8`

**LEAVE VERBATIM (12)** — historically-true incident and provenance records, and the reason
CH3's gate exists at all:
`quota.py:796` · `alert_engine.py:576‑582` (4) · `notices.py:93` · `db.py:771` ·
`scripts/check-quota-refusal-seam.py:13` and `:35` · `tests/test_quota_refusal_seam.py:58` and
`:84` · `tests/test_paywall_daily_block.py:3` and `:103` (they stay verbatim inside the file
after R7 renames it to `tests/test_wall_notice_daily.py`) ·
`audits/TG-BUTTON-UX-W1-endpoint-truth.md` (HISTORICAL — never edited)

**ANNOTATE, not rewrite (1)** — `db.py:221` is the comment block `PAYWALL_HOOK_MIGRATIONS` sits
under, so it takes Q2's `# DEAD since` annotation.

---

## New findings this wave records but does not act on

- **`scripts/check-npm-unlocks.py` runs `docker exec crypto-quant-signal-mcp-postgres-1 psql`** —
  a job a signal-1 deploy can decapitate. It is registered in NEITHER
  `ops/scripts/cron-interlock-registry.json` NOR `ops/monitoring/monitoring-inventory.json`,
  because `scripts/check-cron-interlock-coverage.mjs`'s repo-side corpus is signal-MCP's
  `ops/cron/*.sh` and this job lives in the **bot** repo. Removing the cron closes the gap by
  subtraction; the registry's `active_cron_lines: 118` is a `verified_at`-stamped snapshot
  carrying its own re-derive-never-inherit note, so nothing needs re-stamping. **No signal-MCP
  file is touched.**
- **`UNLOCK_X_FOLLOW_ENABLED` and `ALGOVAULT_SCREENSHOTS_DIR` are both ABSENT** from
  `/etc/algovault-bot/env` (11 vars present, neither is one). R6 closes as "both absent"; the
  file stays untouched — it is the GO-LIVE flip's territory, not this wave's.
- **`src/lib/track-token.ts` still documents itself as the unlock mechanic's verification path**,
  which is how a future wave talks itself into deleting a primitive 15 landing pages and four
  consumers now depend on. That correction belongs to `OPS-TRACK-TOKEN-DOCSTRING-CORRECT-W{NEXT}`.
  **Not opened here.**

## The asymmetry a future reader will misread

The bot's `funnel_events` poll for `first_tool_call_with_track_token` is removed — the **consumer**
side only. The **producer** side in `crypto-quant-signal-mcp` stays, because
`X-AlgoVault-Track-Token` was repurposed from the unlock mechanic's npm-verification path into the
integrations attribution header. This asymmetry is recorded in the `status.md` entry and nowhere
else: `system-map.md` never drew the edge, and inventing a row to hold a note is how a map becomes
a log.

## Host state at probe

| Probe | Value |
|---|---|
| Box UTC | `2026-09-09T06:25:34Z` |
| Cron line | `*/10 * * * * /opt/algovault-bot/scripts/check-npm-unlocks.sh >> /var/log/algovault-bot/check-npm-unlocks.log 2>&1` |
| `crontab -l \| grep -c check-npm-unlocks` | `1` |
| `/var/lib/algovault-bot/screenshots` | exists, 0 files |
| `algovault-bot.service` / `algovault-bot-cron.timer` | `active` / `active` |
| Deploy manifest | `deploy src` · `deploy scripts` · `deploy tests` as **directories** — the per-entry swap propagates deletions |

## Worktree

`/Users/tank/code/.worktrees/algovault-bot/dead-surface-sweep`, branch
`feat/ops-bot-dead-surface-sweep-w1`, off `origin/main 475ddd7`. The primary checkout
`/Users/tank/code/algovault-bot` is **14 commits behind** (`HEAD a770f56`); probing or writing
there is the 2026-08-05 incident where correct citations read as fictional against a stale tree.
Its untracked `uv.lock` is left alone.
