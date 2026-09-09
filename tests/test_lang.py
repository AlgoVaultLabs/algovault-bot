"""OPS-BOT-DEAD-SURFACE-SWEEP-W1 CH1 (2026-09-09): normalize_lang's own suite.

The 9 assertions below are lifted VERBATIM from tests/test_unlock_state_machine.py:182-191,
which CH2 deletes along with the rest of the unlock mechanic. They exist here so the move
of ``normalize_lang`` out of ``unlock.py`` is proven behaviour-preserving BEFORE anything
is deleted — the ordering the whole wave rests on.
"""
from __future__ import annotations

from algovault_bot.lang import normalize_lang


def test_normalize_lang_routing():
    assert normalize_lang(None) == "en"
    assert normalize_lang("") == "en"
    assert normalize_lang("en") == "en"
    assert normalize_lang("en-US") == "en"
    assert normalize_lang("id") == "id"
    assert normalize_lang("id-ID") == "id"
    assert normalize_lang("zh-Hans") == "zh-hans"
    assert normalize_lang("zh-CN") == "zh-hans"
    assert normalize_lang("fr") == "en"  # fallback
