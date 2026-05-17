from __future__ import annotations

import os
import tempfile

import pytest

from algovault_bot.db import Database


@pytest.fixture()
def tmp_db() -> Database:
    fd, path = tempfile.mkstemp(prefix="algovault-bot-test-", suffix=".db")
    os.close(fd)
    db = Database(path)
    yield db
    try:
        os.unlink(path)
        for ext in ("-wal", "-shm", "-journal"):
            if os.path.exists(path + ext):
                os.unlink(path + ext)
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def _stub_symbol_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOT-WATCH-VALIDATE-W1: handlers._validate_symbol normally fires a live
    MCP preflight call against upstream `get_trade_call`. Tests don't have a
    real MCP server, so we stub it to always return None (symbol OK).

    Individual tests that want to exercise the rejection path can override
    via ``monkeypatch.setattr(handlers, "_validate_symbol", lambda *a, **k: "err")``.
    """
    from algovault_bot import handlers
    monkeypatch.setattr(handlers, "_validate_symbol", lambda *a, **k: None)
