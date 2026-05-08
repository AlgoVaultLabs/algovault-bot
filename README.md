# AlgoVault Bot

**Free public Telegram bot — regime alerts + AlgoVault trade calls (BUY/SELL) for AI traders + agents.**

[`@algovaultofficialbot`](https://t.me/algovaultofficialbot) on Telegram. Built by [AlgoVault Labs](https://algovault.com).

---

## What it does

Two kinds of alerts pushed to your watchlist, automatically:

| Alert | Free? | Cadence |
|---|---|---|
| 📊 **Regime shifts** (`TRENDING_UP` / `TRENDING_DOWN` / `RANGING` / `VOLATILE`) | Free, no limit | Per chosen TF, fired only after 2 confirming cycles (no flap) |
| 📈 **Trade calls** (BUY / SELL only — HOLD verdicts are silent) | Counts against your free **100 calls/month** cap | Per chosen TF, real-time |

Free tier covers **all 720+ assets** and **all 11 timeframes** (1m → 1d). You pick what to watch — more assets + lower timeframes = faster quota burn.

The bot is a thin client over the [`crypto-quant-signal-mcp`](https://github.com/AlgoVaultLabs/crypto-quant-signal-mcp) composite-verdict signal API. Every alert traces back to the same on-chain-anchored signal stream that AlgoVault publishes via MCP.

---

## Quick start

1. Open Telegram → search `@algovaultofficialbot` → tap **Start**.
2. Add your first watchlist entry: `/watch BTC 4h`
3. List what you're watching: `/list`
4. Help: `/help`

---

## Commands

```
/start                                   — show welcome message
/watch <COIN> <TF> [EXCHANGE] [TYPE]     — add to watchlist
/unwatch <COIN> <TF> [EXCHANGE]          — remove from watchlist
/list                                    — show your watchlist
/help                                    — full command reference
```

**Arguments:**

- `COIN` — uppercase 2-10 chars (`BTC`, `ETH`, `SOL`, `1000PEPE`, etc.)
- `TF` — `1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d`
- `EXCHANGE` — `HL BINANCE BYBIT OKX BITGET` (default: `BINANCE`)
- `TYPE` — `regime`, `calls`, or `both` (default: `calls`)

**Per-user cap:** 50 watchlist entries.

**Examples:**

```
/watch BTC 4h                  — trade calls only, default exchange
/watch ETH 1h HL regime        — regime-only on Hyperliquid
/watch SOL 15m BYBIT both      — regime + trade calls on Bybit
/unwatch BTC 4h                — remove BTC 4h on BINANCE
```

---

## Quota burn — the math

Trade-call alerts on busier (lower-TF) pairs consume your free 100 calls/month faster:

| Watch | Approx. burn |
|---|---|
| `/watch BTC 1d` | ~1 alert / mo |
| `/watch BTC 4h` | ~5 alerts / mo per pair |
| `/watch BTC 15m` | ~30 alerts / mo per pair |
| `/watch BTC 1m` | quota blown in days |

**Smart routing:**

- HOLD verdicts are silent + free (no alert, no quota tick).
- Regime alerts are always free (no quota tick), no rate cap beyond 20/24h anti-abuse.
- Trade-call alerts (BUY/SELL only) tick your 100/mo counter.

When you hit the cap, [upgrade to Starter ($9.99 → 3,000 calls/mo)](https://api.algovault.com/signup?plan=starter&utm_source=tg_bot&utm_campaign=readme) or pay per call via [x402.org](https://x402.org).

---

## Anti-abuse

Per-user 24h rolling caps:

- 20 regime alerts / 24h
- 30 trade-call alerts / 24h
- 50 trade-call fetches / 24h → bot pauses your trade-call alerts for 24h with one explanatory message (this is to protect free users from blowing through their 100/mo cap on a single noisy day).

These caps don't apply once you upgrade.

Telegram-side: bot uses an `asyncio.Semaphore(25)` to stay under Telegram's 30 msg/sec ceiling.

---

## Privacy

- The bot stores **only**: your `chat_id`, Telegram `username` (if public), Telegram language code, and your watchlist entries.
- No message history. No payment data (signups happen on `algovault.com` via Stripe; the bot never handles PII or cards).
- Bot calls `crypto-quant-signal-mcp` server-side via a single internal-bypass key — your individual `chat_id` is **not** propagated to the upstream signal server.
- All UTM-tagged signups (`utm_source=tg_bot`) live in [Plausible](https://plausible.io/) — operator-side analytics only; no per-user tracking.

---

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │  Hetzner CPX22 (204.168.185.24)             │
                    │                                              │
   You ──Telegram──▶│  algovault-bot.service                      │
                    │    ├── /etc/algovault-bot/env (mode 600)     │
                    │    ├── /var/lib/algovault-bot/state.db       │
                    │    │     SQLite WAL, mode 660                │
                    │    └── /var/log/algovault-bot/alerts.log     │
                    │       (logrotate weekly × 8)                 │
                    │                                              │
                    │  algovault-bot-cron.timer                    │
                    │    fires every 1 min @ HH:MM:00              │
                    │    ↓                                         │
                    │  algovault-bot-cron.service                  │
                    │    per-TF lazy dispatch                      │
                    │    ↓ X-AlgoVault-Internal-Key                │
                    │  http://127.0.0.1:3000/mcp                   │
                    │    crypto-quant-signal-mcp                   │
                    │    (tier:'internal' bypass; bot enforces     │
                    │    per-user quota in its own SQLite)         │
                    └─────────────────────────────────────────────┘
```

- One systemd timer fires the alert engine every minute.
- The engine queries the SQLite watchlist for rows due (per-TF lazy dispatch: `now - last_fetched_at >= TF_SECONDS`).
- Each due row makes a single MCP `tools/call` against `crypto-quant-signal-mcp` over loopback (no Cloudflare round-trip).
- Quota counting and rate-limiting are all bot-side; signal-MCP sees a single internal-bypass tier.

---

## License

MIT. Built by [AlgoVault Labs](https://algovault.com).

Source: this repo. Issues + feature requests: [GitHub Issues](https://github.com/AlgoVaultLabs/algovault-bot/issues).
