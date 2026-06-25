# TG-BUTTON-UX-W1 — Step-0 Endpoint-Truth (Plan-Mode)

Probed live 2026-06-22 against `algovault-bot` `origin/main`. **1 fictional primitive → inline-fixed + flagged; no HALT.** Wave is **algovault-bot ONLY** (zero signal-MCP change).

| # | Spec claim | Reality (probed) | Resolution |
|---|---|---|---|
| 1 | repo `HEAD==origin/main`, clean | local was behind `origin/main 9febfb8` (parallel REFERRAL-INPRODUCT-NUDGE-W1 + REFERRAL-PARITY-NOTIFS-W1); tree clean | branch `feat/tg-button-ux-w1` off `origin/main 9febfb8`; parallel commits touched `alert_engine.py`/`paywall.py`/referral — NOT this wave's files (keyboards/wizard are NEW) → no collision. Baseline pytest 539 GREEN. |
| 2 | PTB ≥21; ConversationHandler/`set_my_commands` first-use | **PTB 22.7**; NO ConversationHandler, NO `set_my_commands` (Menu empty today) | add both — new patterns |
| 3 | `/watch` fn + modes | `handle_watch`→`_commit_watch_combos`→ single: `db.add_watch(...)` via `add_watch_batch` + `messages.watch_added_message`; modes `{regime,calls,both}` (`normalize_alert_type`, default `calls`) | single-watch terminal → `db.add_watch_batch`; replace `watch_added_message` with the card (keep coverage nudge); batch (>1 combo) keeps `batch_watch_added_message` |
| 4 | `/scan` + `/scanwatch` | `handle_scan` one-shot (`_scan_via_mcp`); `handle_scanwatch`→`db.add_scan_watch(chat_id,top_n,tf,exchange,cadence)`; cadence via `_parse_scanwatch_args` (`scan_digest.cadence_for_timeframe`) | scan wizard: one-shot→`handle_scan` (NO card); standing→`db.add_scan_watch` (derive cadence from tf) → card |
| 5 | NO 1m / HIDE_TFS / 3m-floor; 5 exch; ticker→universe | **🚩 `TIMEFRAMES` INCLUDES `1m`; `HIDE_TFS` absent; `normalize_timeframe` accepts `1m`** (premise wrong). EXCHANGES=5 ✓; ticker = `normalize_coin`(COIN_RE)+`_validate_symbol`(MCP); popular = `asset_universe.get_top_assets(n)` | **FLAG+inline-fix:** `keyboards.WIZARD_TIMEFRAMES` = 3m–1d (excludes 1m, wizard-grid UX only); typed `/watch BTC 1m` UNCHANGED; no HIDE_TFS added. Coin grid = `get_top_assets` + 🔤 Type ticker→`normalize_coin`+`_validate_symbol` |
| 6 | i18n | `WELCOME` (HTML, English); `watch_added_message` + adoption buttons English-only (only unlock/referral trilingual) | new strings **English** (match the watch/scan feature-space); `format_subscription_confirmation(..., lang="en")` keeps a `lang` param for forward-compat |
| 7 | `signup_url`+utm | `signup_url(c)`→`api.algovault.com/signup?plan=starter&utm_source=tg_bot&utm_campaign=<c>` (scheme-less); `_UPGRADE_HREF` prepends `https://` | Upgrade url-button = `"https://"+signup_url("start_welcome")` (raw &) |
| 8 | `mnu:/wz:/scn:` no collision | existing `bw:`/`uwa:`/`wb:`/`sw:`/`unlock:`/`unlock_`; referral Share = url button | reserved namespaces free ✓ |
| 9 | `per_message` | wizard mixes callback grids + ForceReply ticker (message) | `ConversationHandler(per_message=False)` (per chat+user; avoids the mixed-handler warning) |
| 10 | test/gate/deploy | pytest 539 green; ruff+mypy pre-push gate; rsync+restart; operator chat for live-send | confirmed |
| 11 | system-map edges | bot Consumes signal-MCP + Produces TG msgs | **NONE — internal only** (no edge/tool/PG col/cron; wizard state in `context.user_data`) |
| 12 | identifier diff | commands, `mnu/wz/scn`, modes, `signup_url` campaign, units | zero drift (only HIDE_TFS premise, flagged) |

**Frozen at end of C1:** `keyboards.py` builders (`main_menu_kb`, `coin_grid_kb(coins,prefix)`, `tf_grid_kb(prefix)`, `exchange_grid_kb(prefix)`, `mode_kb(prefix)`, `confirm_followup_kb`; the `prefix` param lets Watch+Scan reuse the SAME grids); the `mnu:* / wz:* / scn:*` callback scheme `<ns>:<step>:<val>` + `<ns>:back` / `<ns>:cancel` / `wz:type`; `format_subscription_confirmation(kind, *, coin/top_n, tf, exchange, mode/cadence, lang="en")` (extended from the spec draft to serve both watch + scanwatch with ONE renderer).
