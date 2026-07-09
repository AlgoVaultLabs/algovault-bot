# TG-COPY-DEFAULTS-VENUES-W1 — Step-0 Endpoint-Truth

Probed live 2026-07-09 (Plan-Mode, read-only) against `/Users/tank/algovault-bot/` @ `f429541`
(== origin/main) + the production MCP `d2074c47…` (server v1.23.0) + Hetzner `204.168.185.24`.

**Verdict: 1 identifier drift, 0 fictional primitives → NO HALT** (fix drift inline + flag).

| # | Claim | Reality (probed) | Resolution |
|---|---|---|---|
| 1 repo | HEAD==origin/main, clean | `f429541` == `origin/main`; tree clean (untracked `.codegraph/`, `uv.lock` only) | base off `f429541` |
| 2 copy | copy symbols in messages.py | `WELCOME_MESSAGE` (sent **HTML** by `handle_start`/`_start`), `HELP_MESSAGE` (sent **plain** by `_help`), `usage_watch_message()`, `_USAGE_REGIME`/`_USAGE_CALL`/`_USAGE_FUNDING`, `_scanwatch_usage()`, `_parse_scan_args` error in `handle_scan`; `signup_url()` present | replace all; keep `/help` plain-text send |
| 3 bare→wizard | bare `/watch /scan /scanwatch` → wizard | `wizard._entry_command` (watch), `_entry_scan`, `_entry_scanwatch`: `if ctx.args → typed handler; else → _send_coin_grid/_send_topn` (WIZARD). `mnu:*` → separate `_entry_menu` | repoint the 3 **CommandHandler** entries to delegate to the typed handler (default-on-empty); **leave `_entry_menu` untouched** (AC7) |
| 3b defaults | missing→default | `_parse_scan_args`/`_parse_scanwatch_args` **already** default `TOP_N=20, TF=15m, EX=BINANCE` on empty; `_parse_funding_args` **already** defaults 5; `handle_watch` errors on `<2` args; `_parse_coin_tf_exchange` requires coin+tf; `DEFAULT_ALERT_TYPE="calls"`; persist = `_commit_watch_combos` | add default-on-empty to `handle_watch`, `handle_regime`, `handle_call` |
| 4 venues | 5 today; 7 new server-supported | `validators.EXCHANGES` = {HL,BINANCE,BYBIT,OKX,BITGET}; `normalize_exchange` = upper+membership, **no alias map**. **`scan_trade_calls` enum = exactly the 12** (HL BINANCE BYBIT OKX BITGET ASTER BINGX GATE HTX KUCOIN MEXC PHEMEX). LIVE: `get_trade_call` BTC 1h **GATE**→HOLD46, **MEXC**→HOLD48; `get_market_regime` BTC 1h **GATE**→RANGING30 (all `venue_status:promoted`). `scan_funding_arb` cross-venue (no exchange arg) | add 7 tokens + alias map; all 4 commands serve 12 |
| 5 cadence | bare /scanwatch hourly | `cadence_for_timeframe("15m")` = `"1h"` (docstring: "1m–1h→1h, hard-floored"). Cron = `algovault-bot-cron.timer` 1-min lazy per-bucket dispatch → a 1h-cadence scan_watch fires hourly. **Bare /scanwatch already hourly** | **no scheduler change** (AC5 met by defaults) |
| 6 i18n | localized? | `handle_start`/`handle_help` return the module constant verbatim — **English-only, no lang branch** | keep English (match convention) |
| 7 gate | pytest green | **604 passed** pre-edit; ruff + mypy pre-push hook (OPS-BOT-CI-LINT-TYPECHECK-CLEANUP-W1) | edit → re-green |
| 8 system-map | edge-touch | no bot row enumerates venues; `algovault-bot` node Consumes signal-MCP (edge unchanged — the MCP already serves all 12) | **n-a** (do not touch system-map) |
| 9 id-diff | tokens + defaults consistent | venue tokens (12) + all defaults consistent across copy/parser/AC **EXCEPT** Probe-5 says "oi **10** 15m Binance" vs canonical "oi **20** 15m Binance" (default table + R8 + AC5 + /help copy) | **DRIFT → use 20** (dominant + AC-backed); F1 |

## Flags
- **F1 (drift, fixed inline):** bare `/scanwatch` default = **OI · 20 · 15m · Binance** (Probe-5's "10" is a typo; 20 is the value in the default table, R8, AC5, and the /help copy).
- **F2 (surfaced at plan approval):** the verbatim R1 `/start` drops the current clickable HTML Upgrade CTA (`utm_campaign` + `upgrade_from=tg_start`) + the track-record `<a>` → plain text (auto-linked). The exact Upgrade URL is preserved byte-identical in `/help` (R2, `utm_campaign=help_message`). `/start` will be sent as plain text. Approved as-is.
