"""GROWTH-TG-LEVER-ACTIVATION-W1 / CH3 — read-only referral mint-rate accessor.

CH3's original premise was that the referral loop was undiscovered. Step 0
falsified it: `cta.referral_nudge_text` is live and ungated via `alert_engine`,
had already reached 19 of 50 subscribers, and the mint rate was still 1 of 50.
The chapter was halted; this accessor is the one thing retained, as a control
series for the successor activation wave.

Covers CH3 AC 3.5 (read-only, zero schema change) and AC 3.6 (protocol untouched).
"""
from __future__ import annotations

from algovault_bot.db import Database


def test_mint_rate_is_zero_of_zero_on_a_fresh_db(tmp_db: Database) -> None:
    assert tmp_db.referral_mint_rate() == (0, 0)


def test_mint_rate_counts_only_minted_codes(tmp_db: Database) -> None:
    for cid in (1, 2, 3, 4):
        tmp_db.upsert_subscriber(cid, f"u{cid}", "en")
    assert tmp_db.referral_mint_rate() == (0, 4)

    tmp_db.set_referral_code(2, "3NSZ7NIC")
    assert tmp_db.referral_mint_rate() == (1, 4)

    tmp_db.set_referral_code(4, "MSO7LBMA")
    assert tmp_db.referral_mint_rate() == (2, 4)


def test_mint_rate_denominator_includes_non_minters(tmp_db: Database) -> None:
    """The denominator is ALL subscribers, not just engaged ones — the whole point
    of the measurement is that most of the base never mints."""
    for cid in range(1, 51):
        tmp_db.upsert_subscriber(cid, f"u{cid}", "en")
    tmp_db.set_referral_code(7, "XWMV7OE4")
    minted, total = tmp_db.referral_mint_rate()
    assert (minted, total) == (1, 50)  # the live production shape at Step 0


def test_mint_rate_is_read_only(tmp_db: Database) -> None:
    """Calling it must not mutate anything — it is an observation, not a producer."""
    for cid in (1, 2, 3):
        tmp_db.upsert_subscriber(cid, f"u{cid}", "en")
    tmp_db.set_referral_code(1, "AAAA1111")

    before = tmp_db.referral_mint_rate()
    for _ in range(5):
        tmp_db.referral_mint_rate()
    assert tmp_db.referral_mint_rate() == before

    # and it invents no rows and no codes
    assert tmp_db.count_subscribers() == 3
    assert tmp_db.chat_ids_for_referral_code("AAAA1111") == [1]
