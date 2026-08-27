"""TG-BATCH-WATCHLIST-W1 C1 — batch parser + expansion + nudge + uncapped
watchlist + bulk-unwatch.

Pure-layer tests (batch.py expander/decision), DB batch-helper tests, and
command-handler tests (handle_watch/unwatch/unwatchall/list returning the
BatchReply str-subclass). No Telegram sockets; no live MCP (asset universe
injected / monkeypatched).
"""

from __future__ import annotations

import pytest

from algovault_bot import asset_universe, batch, handlers, messages
from algovault_bot.db import Database
from algovault_bot.validators import EXCHANGES, ValidationError
from algovault_bot.quota import (
    FREE_TIER_DAILY_QUOTA,
    FREE_TIER_MONTHLY_QUOTA,
    STARTER_MONTHLY_CALLS,
    STARTER_PRICE_USD,
)



# ── batch.py — pure parse + expand ─────────────────────────────


def test_parse_single_dim() -> None:
    assert batch.parse_coins("BTC", []) == ["BTC"]
    assert batch.parse_timeframes("4h") == ["4h"]
    assert batch.parse_exchanges("binance") == ["BINANCE"]  # normalized upper


def test_parse_comma_list() -> None:
    assert batch.parse_coins("BTC,ETH,SOL", []) == ["BTC", "ETH", "SOL"]
    assert batch.parse_timeframes("15m, 1h") == ["15m", "1h"]  # whitespace-tolerant


def test_parse_all_timeframes_is_the_push_set() -> None:
    # SIGNAL-CLOSEDBAR-FLIP-W1 CH3: `/watch ... all` expands to the PUSH-eligible set, not
    # every timeframe the engine can answer on demand. Asserted against the derived tuple
    # rather than a literal count, so adding or retiring a push timeframe cannot leave this
    # test passing against a stale number.
    tfs = batch.parse_timeframes("all")
    assert tfs == list(batch.PUSH_TF_ORDER)
    assert "1m" not in tfs, "1m cannot be SCHEDULED — see validators.PUSH_TIMEFRAMES"
    assert tfs[0] == "3m" and tfs[-1] == "1d"  # canonical ascending order, push floor at 3m


def test_parse_all_exchanges_is_12() -> None:
    xs = batch.parse_exchanges("all")
    assert len(xs) == 12  # TG-COPY-DEFAULTS-VENUES-W1: 5 → 12 venues
    assert set(xs) == set(EXCHANGES)


def test_parse_all_coins_uses_injected_universe() -> None:
    universe = ["BTC", "ETH", "SOL", "ZEC"]
    assert batch.parse_coins("all", universe) == universe


def test_expand_cartesian_count() -> None:
    combos = batch.expand_watch_spec("BTC,ETH", "15m,1h", "BINANCE,BYBIT", universe=[])
    assert len(combos) == 8  # 2 × 2 × 2
    assert ("BTC", "15m", "BINANCE") in combos
    assert ("ETH", "1h", "BYBIT") in combos


def test_expand_dedup() -> None:
    combos = batch.expand_watch_spec("BTC,BTC", "4h", "BINANCE", universe=[])
    assert combos == [("BTC", "4h", "BINANCE")]


def test_expand_all_all_all_for_one_coin() -> None:
    # AC1.1 shape: BTC all all = 1 × |push TFs| × 12 venues. Was 11 TFs (=132) until
    # SIGNAL-CLOSEDBAR-FLIP-W1 CH3 dropped 1m from the push set → 10 TFs (=120).
    combos = batch.expand_watch_spec("BTC", "all", "all", universe=[])
    assert len(combos) == len(batch.PUSH_TF_ORDER) * 12 == 120


def test_expand_invalid_token_raises() -> None:
    with pytest.raises(ValidationError):
        batch.parse_coins("BTC-USD", [])
    with pytest.raises(ValidationError):
        batch.parse_timeframes("5h")
    with pytest.raises(ValidationError):
        batch.parse_exchanges("kraken")


# ── batch.py — nudge decision (adjustment 3) ───────────────────


def test_should_confirm_over_threshold() -> None:
    assert batch.should_confirm(55, "BTC", 50) is True


def test_should_confirm_coins_all_even_if_small() -> None:
    assert batch.should_confirm(3, "all", 50) is True


def test_should_confirm_bounded_btc_all_does_not_nudge() -> None:
    # /watch BTC all = |push TFs| combos, coins != "all" → commit inline, NO nudge.
    assert batch.should_confirm(len(batch.PUSH_TF_ORDER), "BTC", 50) is False


def test_should_confirm_comma_list_under_threshold() -> None:
    assert batch.should_confirm(8, "BTC,ETH", 50) is False


# ── asset_universe (injected fake MCP) ─────────────────────────


class _FakeMcp:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read_resource(self, _uri: str) -> dict:
        return self.payload


_PERF = {
    "byAsset": {
        "BTC": {"count": 500, "tier": 1},
        "ETH": {"count": 300, "tier": 1},
        "SOL": {"count": 120, "tier": 2},
        "ZEC": {"count": 40, "tier": 3},
    }
}


def test_asset_universe_lists_all_keys() -> None:
    asset_universe._reset_cache_for_test()
    uni = asset_universe.get_asset_universe(mcp=_FakeMcp(_PERF))
    assert set(uni) == {"BTC", "ETH", "SOL", "ZEC"}


def test_top_assets_ranked_by_count_desc() -> None:
    asset_universe._reset_cache_for_test()
    top = asset_universe.get_top_assets(2, mcp=_FakeMcp(_PERF))
    assert top == ["BTC", "ETH"]  # by count desc


# ── db batch helpers ───────────────────────────────────────────


def test_add_watch_batch_inserts_and_is_idempotent(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    combos = [("BTC", "4h", "BINANCE"), ("ETH", "1h", "BINANCE"), ("SOL", "15m", "BYBIT")]
    inserted = tmp_db.add_watch_batch(1, combos, "calls")
    assert inserted == 3
    assert tmp_db.count_watches(1) == 3
    # Re-add same batch → no new rows (PK conflict → upsert), count stable.
    again = tmp_db.add_watch_batch(1, combos, "both")
    assert again == 0
    assert tmp_db.count_watches(1) == 3
    rows = {(r["coin"], r["timeframe"], r["exchange"]): r["alert_type"] for r in tmp_db.list_watches(1)}
    assert rows[("BTC", "4h", "BINANCE")] == "both"  # alert_type updated on conflict


def test_remove_watch_batch_coin_wildcard(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch_batch(1, [("BTC", "4h", "BINANCE"), ("BTC", "1h", "BYBIT"), ("ETH", "4h", "BINANCE")], "calls")
    removed = tmp_db.remove_watch_batch(1, coin="BTC")
    assert removed == 2
    assert tmp_db.count_watches(1) == 1  # only ETH left


def test_remove_watch_batch_tf_wildcard(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch_batch(1, [("BTC", "1m", "BINANCE"), ("ETH", "1m", "BYBIT"), ("SOL", "4h", "OKX")], "calls")
    removed = tmp_db.remove_watch_batch(1, timeframe="1m")
    assert removed == 2
    assert tmp_db.count_watches(1) == 1


def test_remove_all_watches_scoped_to_user(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.upsert_subscriber(2, "v", "en")
    tmp_db.add_watch_batch(1, [("BTC", "4h", "BINANCE"), ("ETH", "1h", "BINANCE")], "calls")
    tmp_db.add_watch_batch(2, [("SOL", "15m", "BYBIT")], "calls")
    removed = tmp_db.remove_all_watches(1)
    assert removed == 2
    assert tmp_db.count_watches(1) == 0
    assert tmp_db.count_watches(2) == 1  # other user's rows intact


# ── handle_watch — batch + nudge + uncapped ────────────────────


def test_handle_watch_comma_list_commits_no_nudge(tmp_db: Database) -> None:
    # AC1.2 — 3 coins, one TF, explicit exchange, ≤ threshold, no `all` → commit.
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC,ETH,SOL", "15m", "BINANCE"])
    assert reply.confirm is False
    assert tmp_db.count_watches(1) == 3
    assert "3" in reply  # summary mentions the count


def test_handle_watch_all_dim_triggers_nudge_no_insert(tmp_db: Database) -> None:
    # AC1.1 — BTC all all = 1 x |push TFs| x 12 venues > threshold → nudge, nothing
    # inserted yet. 132 → 120 since CH3 dropped 1m from the push set.
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC", "all", "all"])
    assert reply.confirm is True
    assert reply.combos == len(batch.PUSH_TF_ORDER) * 12 == 120
    assert tmp_db.count_watches(1) == 0
    assert reply.pending is not None


def test_handle_watch_btc_all_commits_inline(tmp_db: Database) -> None:
    # adjustment 3 — /watch BTC all = |push TFs| combos <= threshold, coins != all →
    # commit, no nudge. 11 → 10 since CH3 dropped 1m from the push set.
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC", "all"])
    assert reply.confirm is False
    assert tmp_db.count_watches(1) == len(batch.PUSH_TF_ORDER)


def test_handle_watch_all_coins_triggers_nudge(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    # AC1.3 — `all` coins → nudge regardless of combo count.
    monkeypatch.setattr(asset_universe, "get_asset_universe", lambda mcp=None: ["BTC", "ETH", "SOL"])
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", ["all", "1h", "BINANCE"])
    assert reply.confirm is True
    assert reply.combos == 3
    assert tmp_db.count_watches(1) == 0


def test_handle_watch_51st_accepted_cap_removed(tmp_db: Database) -> None:
    # AC1.4 — cap removed: 51 distinct combos all accepted, no rejection.
    coins = ",".join(f"COIN{i:02d}" for i in range(51))
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", [coins, "4h", "BINANCE"])
    assert reply.confirm is True  # 51 > 50 → nudge (then commit via callback)
    committed = handlers.commit_watch_batch(tmp_db, 1, reply.pending, "add")
    assert tmp_db.count_watches(1) == 51
    assert "cap" not in committed.lower()


def test_commit_watch_batch_top_n_clamps(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    # AC1.3 — [Top 30 only] clamps `all` coins to top-N by activity.
    monkeypatch.setattr(asset_universe, "get_asset_universe", lambda mcp=None: [f"C{i}" for i in range(100)])
    monkeypatch.setattr(asset_universe, "get_top_assets", lambda n, mcp=None: [f"C{i}" for i in range(n)])
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", ["all", "1h", "BINANCE"])
    assert reply.confirm is True
    handlers.commit_watch_batch(tmp_db, 1, reply.pending, "top")
    assert tmp_db.count_watches(1) == 30  # top-30 × 1h × BINANCE


# ── bulk unwatch ───────────────────────────────────────────────


def test_handle_unwatch_coin_wildcard(tmp_db: Database) -> None:
    # AC1.6 — /unwatch BTC all removes every BTC row.
    handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC", "all"])  # |push TFs| BTC rows
    handlers.handle_watch(tmp_db, 1, "u", "en", ["ETH", "4h", "BINANCE"])
    reply = handlers.handle_unwatch(tmp_db, 1, "u", "en", ["BTC", "all"])
    assert "🗑️" in reply
    assert tmp_db.count_watches(1) == 1  # only ETH left


def test_handle_unwatch_tf_wildcard(tmp_db: Database) -> None:
    # AC1.6 — /unwatch all <TF> removes every row on that TF. Subject moved 1m → 3m by
    # SIGNAL-CLOSEDBAR-FLIP-W1 CH3: 1m can no longer be watched, so a 1m fixture would be
    # testing the rejection path, not the wildcard.
    handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC,ETH", "3m", "BINANCE"])
    handlers.handle_watch(tmp_db, 1, "u", "en", ["SOL", "4h", "BINANCE"])
    reply = handlers.handle_unwatch(tmp_db, 1, "u", "en", ["all", "3m"])
    assert "🗑️" in reply
    assert tmp_db.count_watches(1) == 1


def test_handle_unwatchall_confirms_then_commits(tmp_db: Database) -> None:
    # AC1.5 — /unwatchall confirms, then deletes everything. Fixture TF moved off 1m by
    # SIGNAL-CLOSEDBAR-FLIP-W1 CH3 — a 1m watch is now refused, so the old fixture created
    # ZERO rows and the test passed vacuously on an empty watchlist.
    handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC,ETH", "3m", "BINANCE"])
    reply = handlers.handle_unwatchall(tmp_db, 1, "u", "en")
    assert reply.confirm is True
    assert tmp_db.count_watches(1) == 2  # not deleted yet
    done = handlers.commit_unwatchall(tmp_db, 1)
    assert "2" in done  # "Cleared your watchlist — removed 2 watches."
    assert tmp_db.count_watches(1) == 0
    assert "empty" in handlers.handle_list(tmp_db, 1, "u", "en")  # /list now empty


# ── /list summarization ────────────────────────────────────────


def test_handle_list_summarizes_over_threshold(tmp_db: Database) -> None:
    # AC1.7 — 200 rows → grouped summary, not 200 lines.
    coins = ",".join(f"AB{i:03d}" for i in range(200))
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", [coins, "4h", "BINANCE"])
    assert reply.confirm is True  # 200 > threshold → nudge, then commit
    handlers.commit_watch_batch(tmp_db, 1, reply.pending, "add")
    assert tmp_db.count_watches(1) == 200
    reply = handlers.handle_list(tmp_db, 1, "u", "en")
    assert "200" in reply
    assert reply.count("\n") < 60  # grouped, not one line per row


# ── /start + /help copy (AC1.9) ────────────────────────────────


def test_start_copy_plain_language_and_link_light() -> None:
    # TG-COPY-DEFAULTS-VENUES-W1 (R1/F2): plain-language /start; link-light — the
    # clickable Upgrade CTA + its utm moved to /help.
    w = messages.welcome_message(FREE_TIER_MONTHLY_QUOTA, FREE_TIER_DAILY_QUOTA, STARTER_PRICE_USD, STARTER_MONTHLY_CALLS)
    assert "the brain layer for AI trading agents" in w
    assert "900+ markets (crypto, gold, stocks, pre-IPO) across 12 exchanges" in w
    assert "New here? Just type /watch and I'll start you on BTC 1h (Binance)." in w
    assert "🔔 Watch a coin → /watch BTC 4h" in w
    assert "🔍 Scan the top movers → /scan" in w
    assert "📈 Get one call now → /call ETH 1h" in w
    # F2: plain text — no HTML tags, no inline upgrade link/utm on /start
    assert "<a href" not in w
    assert "utm_campaign=start_welcome" not in w
    assert "algovault.com/track-record" in w


def test_help_copy_has_batch_and_unwatchall() -> None:
    h = messages.help_message(FREE_TIER_MONTHLY_QUOTA, FREE_TIER_DAILY_QUOTA)
    assert "/unwatchall" in h
    assert "all" in h  # batch syntax hint
    assert "[regime|calls|both]" in h
