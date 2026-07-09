# TG-SCANWATCH-TF-CADENCE-AND-HELP-BTN-W1 — Step-0 Endpoint-Truth

Probed live 2026-07-09 (Plan-Mode, read-only) @ `a422816` == origin/main.
**Part A HALT'd (4 contradicted primitives) → architect chose Approach B** (dispatch on the TF
directly; leave `cadence_for_timeframe` + the cadence column + the MCP mirror untouched — no
migration, no cross-repo divergence). **Part B shipped now.**

| # | Claim (spec) | Reality (probed) | Resolution |
|---|---|---|---|
| 1 repo | HEAD==origin/main clean | `a422816` == origin/main, clean | ✓ base off a422816 |
| 2 cadence fn | "correct ONE function to identity" | `scan_digest.py` is a faithful cross-repo **MIRROR of the MCP `src/lib/scan-digest.ts`** ("same map, pinned by construction"); MCP out-of-scope. `VALID_CADENCES=("1h","4h","1d")`, `_CADENCE_SECONDS` defines only those 3 | **CONTRADICTED** → Approach B leaves it UNTOUCHED |
| 2b consumers | — | only `handlers.py` (`_parse_scanwatch_args`); **`/watch` uses `TF_SECONDS` lazy dispatch, NOT `cadence_for_timeframe`** | ✓ /watch unaffected |
| 3 eval tick | ≤1m to honor sub-hourly | `algovault-bot-cron.timer` **`OnCalendar=*:*:00` (every minute)** → `main()`→`process_scan_digests`; per-scan_watch bucket gates delivery | ✓ **tick fine — no parallel loop, no hourly-timer HALT** |
| 4 cadence storage | (identity feeds it) | `scan_watches.cadence` = `TEXT NOT NULL DEFAULT '1h' CHECK (cadence IN ('1h','4h','1d'))`; `add_scan_watch` INSERTs it; `cadence_bucket_epoch` does `_CADENCE_SECONDS[cadence]` | **CONTRADICTED** — identity value fails CHECK + KeyErrors → B buckets on the TF instead (column stays vestigial) |
| 5 cadence-arg | — | `_parse_scanwatch_args` validates the CADENCE arg via `is_valid_cadence` → "must be one of 1h/4h/1d" | **CONTRADICTED** — B: dispatch uses the TF; the arg/column are vestigial; copy states the TF |
| 6 migration | "none expected here" | widening the CHECK ⇒ backed-up `scan_watches` table-rebuild | **CONTRADICTED** — B avoids it (additive `last_sent_sig` column only) |
| 7 dispatch bucket | — | 3 sites in `process_scan_digests`: `cadence_bucket_epoch(r["cadence"], now)` @ alert_engine.py:820 (due), :868 (all-HOLD mark), :877 (deliver) | swap → `timeframe_bucket_epoch(r["timeframe"], now)` |
| 8 coalesce/dedup | reuse existing | coalesce EXISTS (group by `(top_n, tf, exchange, rank_by)`); suppress-empty EXISTS; **content-dedup does NOT** (persistent BUY re-sends each bucket) | ADD per-scanwatch `last_sent_sig` (spec R3) |
| 9 /start button | reusable builder | inline+hardcoded `keyboards.main_menu_kb`: `InlineKeyboardButton("⬆️ Upgrade", url="https://"+signup_url("start_welcome"))` | extract `upgrade_button(campaign)`; route /start + /help |
| 10 system-map | NONE | cadence-dispatch + additive column + copy/markup — no cross-component edge | **n-a** |

## Approach B (architect-chosen)
Dispatch scanwatch on the **scan_watch's timeframe** (`timeframe_bucket_epoch` via `TF_SECONDS`), not the coarse `cadence` column. `cadence_for_timeframe` / `_CADENCE_SECONDS` / `VALID_CADENCES` / `is_valid_cadence` stay byte-unchanged (MCP mirror intact). The `cadence` column keeps storing 1h/4h/1d (valid CHECK, vestigial for dispatch). Content-dedup via a NEW additive `last_sent_sig` column so a persistent BUY fires once per change (makes the faster cadence timely, not spammy). AC1/AC2 reworded to the outcome: **the scan_watch's timeframe is the sole dispatch-cadence source.**
