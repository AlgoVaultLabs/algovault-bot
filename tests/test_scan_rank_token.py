"""SCAN-RANKBY-W1 CH3 — /scan + /scanwatch rank token + scan_watches rank_by migration.

Covers: the row-preserving + idempotent + orphan-safe PK-widen migration; rank_by as part
of the watch identity (oi + nfr coexist); per-lens fire-marking; the raw-token forward to
scan_trade_calls; alias/canonical recognition; the friendly invalid-token error; /scanwatch
persistence; and byte-identical /scan when no lens is given.
"""
from __future__ import annotations

import sqlite3

import pytest

from algovault_bot import capabilities, handlers
from algovault_bot.db import Database, _migrate_scan_watches_rank_by

# The pre-wave scan_watches shape (PK without rank_by) — seeds the migration tests.
OLD_SCAN_WATCHES_SQL = (
    "CREATE TABLE scan_watches ("
    "  chat_id INTEGER NOT NULL, top_n INTEGER NOT NULL DEFAULT 20,"
    "  timeframe TEXT NOT NULL DEFAULT '15m', exchange TEXT NOT NULL DEFAULT 'BINANCE',"
    "  cadence TEXT NOT NULL DEFAULT '1h', last_fired_bucket INTEGER NOT NULL DEFAULT 0,"
    "  added_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),"
    "  PRIMARY KEY (chat_id, top_n, timeframe, exchange))"
)
_OLD_ROWS = [
    (1, 20, "15m", "BINANCE", "1h", 100),
    (2, 10, "4h", "BYBIT", "4h", 200),
]


@pytest.fixture(autouse=True)
def _rank_lenses_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic, no-network lens set (the hardcoded Tier-3 fallback)."""
    monkeypatch.setattr(capabilities, "rank_lenses", lambda **k: capabilities._FALLBACK_RANK_LENSES)
    capabilities._reset_rank_lenses_cache()


# ── migration ──────────────────────────────────────────────────────────────

def test_migration_preserves_rows_backfills_oi_and_backs_up(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit, FK off (like _connect minus FK)
    conn.execute(OLD_SCAN_WATCHES_SQL)
    conn.executemany(
        "INSERT INTO scan_watches(chat_id, top_n, timeframe, exchange, cadence, last_fired_bucket) "
        "VALUES (?,?,?,?,?,?)",
        _OLD_ROWS,
    )
    cur = conn.cursor()
    pre = cur.execute("SELECT COUNT(*) FROM scan_watches").fetchone()[0]
    _migrate_scan_watches_rank_by(cur)
    cols = [r[1] for r in cur.execute("PRAGMA table_info(scan_watches)").fetchall()]
    assert "rank_by" in cols
    # rank_by joined the PK (pk flag is column index 5 in table_info)
    pk_cols = {r[1] for r in cur.execute("PRAGMA table_info(scan_watches)").fetchall() if r[5]}
    assert pk_cols == {"chat_id", "top_n", "timeframe", "exchange", "rank_by"}
    post = cur.execute("SELECT COUNT(*) FROM scan_watches").fetchone()[0]
    assert post == pre == 2  # row-preserving
    assert {r[0] for r in cur.execute("SELECT DISTINCT rank_by FROM scan_watches")} == {"oi"}  # backfilled
    assert {r[0] for r in cur.execute("SELECT last_fired_bucket FROM scan_watches")} == {100, 200}  # state kept
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "scan_watches_backup_rankby" in tables  # rollback artifact
    assert "scan_watches_pre_rankby" not in tables  # old table dropped
    conn.close()


def test_migration_idempotent(tmp_path):
    path = str(tmp_path / "old2.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(OLD_SCAN_WATCHES_SQL)
    conn.executemany(
        "INSERT INTO scan_watches(chat_id, top_n, timeframe, exchange, cadence, last_fired_bucket) "
        "VALUES (?,?,?,?,?,?)",
        _OLD_ROWS,
    )
    cur = conn.cursor()
    _migrate_scan_watches_rank_by(cur)
    _migrate_scan_watches_rank_by(cur)  # second call = no-op (rank_by present), no error
    assert cur.execute("SELECT COUNT(*) FROM scan_watches").fetchone()[0] == 2
    conn.close()


def test_init_schema_migrates_old_db_orphan_safe(tmp_path):
    """Full _init_schema path on an EXISTING old-shape DB with an ORPHAN row (chat_id with
    no subscriber) — the FK-off recreate must PRESERVE it, never crash boot."""
    path = str(tmp_path / "orphan.db")
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute(OLD_SCAN_WATCHES_SQL)
    raw.executemany(
        "INSERT INTO scan_watches(chat_id, top_n, timeframe, exchange, cadence, last_fired_bucket) "
        "VALUES (?,?,?,?,?,?)",
        _OLD_ROWS,  # neither chat_id is in subscribers → orphans
    )
    raw.close()
    db = Database(path)  # _init_schema runs the migration (FK on globally; off during recreate)
    rows = db.list_all_scan_watches()
    assert len(rows) == 2
    assert {r["rank_by"] for r in rows} == {"oi"}
    assert {r["last_fired_bucket"] for r in rows} == {100, 200}
    # re-init is idempotent
    assert len(Database(path).list_all_scan_watches()) == 2


def test_fresh_db_has_rank_by_in_pk(tmp_db):
    with tmp_db._cursor() as cur:
        pk_cols = {r[1] for r in cur.execute("PRAGMA table_info(scan_watches)").fetchall() if r[5]}
    assert "rank_by" in pk_cols


# ── rank_by as watch identity ────────────────────────────────────────────────

def test_oi_and_nfr_coexist_and_remove_is_lens_specific(tmp_db):
    tmp_db.upsert_subscriber(1, "u", "en")
    assert tmp_db.add_scan_watch(1, 20, "15m", "BINANCE", "1h", "oi") is True
    assert tmp_db.add_scan_watch(1, 20, "15m", "BINANCE", "1h", "nfr") is True  # distinct PK row
    assert {r["rank_by"] for r in tmp_db.list_scan_watches(1)} == {"oi", "nfr"}
    assert tmp_db.remove_scan_watch(1, 20, "15m", "BINANCE", "nfr") is True
    assert {r["rank_by"] for r in tmp_db.list_scan_watches(1)} == {"oi"}


def test_mark_fired_targets_only_the_lens(tmp_db):
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_scan_watch(1, 20, "15m", "BINANCE", "1h", "oi")
    tmp_db.add_scan_watch(1, 20, "15m", "BINANCE", "1h", "nfr")
    tmp_db.mark_scan_watch_fired(1, 20, "15m", "BINANCE", 999, "nfr")
    fired = {r["rank_by"]: r["last_fired_bucket"] for r in tmp_db.list_all_scan_watches()}
    assert fired["nfr"] == 999
    assert fired["oi"] == 0  # the oi watch is untouched


def test_add_scan_watch_default_is_oi(tmp_db):
    tmp_db.upsert_subscriber(5, "u", "en")
    tmp_db.add_scan_watch(5, 20, "15m", "BINANCE", "1h")  # 5-arg back-compat
    assert tmp_db.list_scan_watches(5)[0]["rank_by"] == "oi"


# ── parse + forward (the bot forwards the RAW token; MCP resolves) ────────────

def test_parse_recognizes_canonical_and_alias():
    for tok in ("gainers", "gain", "funding_negative", "nfr", "oi", "vol"):
        top_n, tf, exch, rank = handlers._parse_scan_args([tok])
        assert rank == tok  # forwarded raw


def test_scan_forwards_raw_rank(tmp_db, monkeypatch):
    captured: dict = {}

    def fake(top_n, tf, exchange, rank=None):
        captured.update(rank=rank, top_n=top_n, exchange=exchange)
        return {"calls": [{"coin": "BTC", "call": "BUY", "confidence": 80, "regime": "TRENDING_UP"}]}

    monkeypatch.setattr(handlers, "_scan_via_mcp", fake)
    handlers.handle_scan(tmp_db, 1, "u", "en", ["nfr", "20", "15m", "binance"])
    assert captured == {"rank": "nfr", "top_n": 20, "exchange": "BINANCE"}


def test_scan_no_rank_is_none_byte_identical(tmp_db, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(handlers, "_scan_via_mcp",
                        lambda t, f, e, r=None: (captured.update(r=r), {"calls": []})[1])
    handlers.handle_scan(tmp_db, 1, "u", "en", ["20", "15m"])
    assert captured["r"] is None  # omitted ⇒ None ⇒ MCP default oi


def test_scan_invalid_token_friendly_error(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, "_scan_via_mcp", lambda *a: {"calls": []})
    out = handlers.handle_scan(tmp_db, 1, "u", "en", ["garbage"])
    assert "unrecognized argument" in out
    assert "Lenses:" in out  # derived lens list surfaced (not hardcoded in the handler)


def test_scanwatch_persists_rank(tmp_db):
    tmp_db.upsert_subscriber(1, "u", "en")
    handlers.handle_scanwatch(tmp_db, 1, "u", "en", ["nfr", "4h"])
    rows = tmp_db.list_scan_watches(1)
    assert len(rows) == 1 and rows[0]["rank_by"] == "nfr"


# ── SCAN-RANKBY-W2: the bot auto-inherits the `volatility`/`atr` lens via /capabilities ──

def test_w2_atr_lens_recognized_and_forwarded(tmp_db, monkeypatch):
    captured: dict = {}

    def fake(top_n, tf, exchange, rank=None):
        captured["rank"] = rank
        return {"calls": []}

    monkeypatch.setattr(handlers, "_scan_via_mcp", fake)
    handlers.handle_scan(tmp_db, 1, "u", "en", ["atr", "20", "15m"])
    assert captured["rank"] == "atr"  # forwarded RAW; the MCP resolves atr → volatility


def test_w2_volatility_and_atr_in_derived_token_set():
    toks = capabilities.recognized_rank_tokens()
    assert "volatility" in toks and "atr" in toks
    assert capabilities.rank_label("atr") == "ATRP (volatility)"


def test_w2_scanwatch_persists_volatility(tmp_db):
    tmp_db.upsert_subscriber(2, "u", "en")
    handlers.handle_scanwatch(tmp_db, 2, "u", "en", ["atr", "1h"])
    rows = tmp_db.list_scan_watches(2)
    assert len(rows) == 1 and rows[0]["rank_by"] == "atr"  # raw token persisted; MCP resolves at fire


# ── SCAN-RANKBY-W3: the bot auto-inherits the `oi_change`/`oid` lens via /capabilities ──

def test_w3_oid_lens_recognized_and_forwarded(tmp_db, monkeypatch):
    captured: dict = {}

    def fake(top_n, tf, exchange, rank=None):
        captured["rank"] = rank
        return {"calls": []}

    monkeypatch.setattr(handlers, "_scan_via_mcp", fake)
    handlers.handle_scan(tmp_db, 1, "u", "en", ["oid", "20", "15m"])
    assert captured["rank"] == "oid"  # forwarded RAW; the MCP resolves oid → oi_change


def test_w3_oi_change_and_oid_in_derived_token_set():
    toks = capabilities.recognized_rank_tokens()
    assert "oi_change" in toks and "oid" in toks
    assert capabilities.rank_label("oid") == "OI change (24h)"


def test_w3_scanwatch_persists_oid(tmp_db):
    tmp_db.upsert_subscriber(3, "u", "en")
    handlers.handle_scanwatch(tmp_db, 3, "u", "en", ["oid", "4h"])
    rows = tmp_db.list_scan_watches(3)
    assert len(rows) == 1 and rows[0]["rank_by"] == "oid"  # raw token persisted; MCP resolves at fire
