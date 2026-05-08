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
