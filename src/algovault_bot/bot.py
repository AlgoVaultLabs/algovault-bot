"""AlgoVault Telegram Bot — entry point.

C1 scope: bot.Application + /start handler + polling loop. No watchlists, no
alert engine, no rate-limits — those land in C2/C3/C5.
"""

from __future__ import annotations

import logging
import os
import sys

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes


# Verbatim per spec C1 (with D3-A URL rewrite: api.algovault.com/signup?plan=starter&...).
# The 4 /watch example lines, the trade-calls quota line, and the signup URL must
# match AC1.3 byte-for-byte.
WELCOME_MESSAGE = (
    "👋 Welcome to AlgoVault — the brain layer for AI trading agents.\n"
    "\n"
    "I push two kinds of alerts to your watchlist:\n"
    "📊 Regime shifts — free, no limit\n"
    "📈 Trade calls (BUY/SELL) — counts against your free 100 calls/month\n"
    "HOLD verdicts are silent + free.\n"
    "\n"
    "Free tier covers all 710+ assets and all 11 timeframes (1m–1d). YOU choose what to watch.\n"
    "\n"
    "More assets + lower timeframes = faster quota burn. Examples:\n"
    "• /watch BTC 1d        — slow burn (~1 alert/mo)\n"
    "• /watch BTC 4h        — moderate (~5 alerts/mo per pair)\n"
    "• /watch BTC 15m       — fast (~30 alerts/mo per pair)\n"
    "• /watch BTC 1m        — very fast (cap blown in days)\n"
    "\n"
    "Get started:\n"
    "/watch <COIN> <TF>     — add to watchlist\n"
    "/list                  — see your picks\n"
    "/help                  — full commands\n"
    "\n"
    "Hit the cap? Upgrade to Starter ($9.99 → 3,000 calls/mo) or pay per call via x402.\n"
    "→ api.algovault.com/signup?plan=starter&utm_source=tg_bot&utm_campaign=start_welcome"
)


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # python-telegram-bot is chatty at INFO; keep it at WARNING unless LOG_LEVEL=DEBUG.
    if level.upper() != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)


async def start_handler(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    await update.message.reply_text(WELCOME_MESSAGE, disable_web_page_preview=True)


def build_application(token: str) -> Application:
    app: Application = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_handler))
    return app


def main() -> None:
    token = os.environ.get("PUBLIC_BOT_TOKEN", "").strip()
    if not token:
        sys.stderr.write("FATAL: PUBLIC_BOT_TOKEN not set in environment\n")
        sys.exit(2)

    _setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    log = logging.getLogger("algovault_bot")
    log.info("starting algovault-bot polling loop")

    app = build_application(token)
    # run_polling blocks; signal handlers stop it cleanly under systemd.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
