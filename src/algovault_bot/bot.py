"""algovault-bot entry point.

C1: bot service skeleton + /start handler + polling loop.
C2: + /watch /unwatch /list /help with SQLite persistence.
"""

from __future__ import annotations

import logging
import os
import sys

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from .db import Database, DEFAULT_DB_PATH
from .handlers import register_handlers
from .messages import WELCOME_MESSAGE  # noqa: F401 — re-exported for tests/import paths


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if level.upper() != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)


def build_application(token: str, db_path: str = DEFAULT_DB_PATH) -> Application:
    app: Application = ApplicationBuilder().token(token).build()
    db = Database(db_path)
    register_handlers(app, db)
    return app


def main() -> None:
    token = os.environ.get("PUBLIC_BOT_TOKEN", "").strip()
    if not token:
        sys.stderr.write("FATAL: PUBLIC_BOT_TOKEN not set in environment\n")
        sys.exit(2)

    db_path = os.environ.get("ALGOVAULT_BOT_DB_PATH", DEFAULT_DB_PATH)
    _setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    log = logging.getLogger("algovault_bot")
    log.info("starting algovault-bot polling loop (db=%s)", db_path)

    app = build_application(token, db_path)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
