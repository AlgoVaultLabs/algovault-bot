"""The pre-push hook reaches its verdict `case` for all three tokens.

GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1 R4.

WHY THIS FILE EXISTS. `hooks/pre-push` documents, in three separate comments, that it "GATES ON
THE TOKEN, NEVER THE EXIT CODE" and that "INDETERMINATE does not block … It is LOUD instead".
Both statements were FALSE in practice. Line 14 is `set -euo pipefail`, and the seam gate's
output was captured with a plain `SEAM_OUT="$(…)"` — under `set -e` a failing command
substitution in an assignment aborts the shell AT THAT LINE, so the `case` beneath it was dead
code for every non-zero exit. Measured before the fix: a FAIL exited 1 printing NOTHING (neither
the gate's diagnostic nor "push refused"), and an INDETERMINATE exited 3 and BLOCKED the push.

The defect predates this wave and survived because no leg had ever emitted a non-PASS verdict —
it was latent until the first one did. V2 CH1 R4 ships legs that can (L6 and the `followup`
shape), which is exactly when a silent, diagnostic-free push refusal would have appeared.

WHAT IS ASSERTED: the hook's own seam block, extracted verbatim from the committed file and run
against a STUB gate, must reach its `case` for PASS / FAIL / INDETERMINATE and produce the
documented behaviour for each. Reading the block out of the real file rather than restating it
is the point — a copy would drift, and this test would then be verifying a hook nobody runs.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "pre-push"


def _seam_block() -> str:
    """The hook's seam block, from `SEAM_PY=` to the end of its `case`. Read, never restated."""
    src = HOOK.read_text(encoding="utf-8")
    start = src.index('SEAM_PY="')
    end = src.index("esac", start) + len("esac")
    return src[start:end]


def test_the_hook_still_sets_e_so_this_test_is_not_vacuous() -> None:
    """If someone drops `set -euo pipefail`, the hazard is gone and so is this test's subject.

    A vacuity guard at the CONSTRUCTION site: without `set -e` every case below would pass for a
    reason that has nothing to do with the fix, and the suite would report a guard it no longer
    has. Assert the hazard exists before asserting it is handled.
    """
    assert re.search(r"^set -euo pipefail$", HOOK.read_text(encoding="utf-8"), re.M)


def test_the_capture_tolerates_a_non_zero_gate() -> None:
    """The one-line fix itself, pinned so it cannot be 'tidied' away."""
    assert "|| true)" in _seam_block(), (
        "without `|| true` the token-reading case is dead code under set -e"
    )


@pytest.mark.parametrize(
    ("verdict", "gate_rc", "want_rc", "want_in_output"),
    [
        ("PASS", 0, 0, "QUOTA_REFUSAL_SEAM_VERDICT=PASS"),
        # a FAIL must refuse AND say why — the pre-fix behaviour was rc=1 with no output at all
        ("FAIL", 1, 1, "push refused"),
        # an INDETERMINATE must be LOUD and NOT block, which is what the hook promises
        ("INDETERMINATE", 3, 0, "verified nothing"),
    ],
)
def test_every_verdict_reaches_the_case(
    tmp_path: Path, verdict: str, gate_rc: int, want_rc: int, want_in_output: str
) -> None:
    stub = tmp_path / "stub_gate.sh"
    stub.write_text(
        f'#!/bin/sh\necho "QUOTA_REFUSAL_SEAM_VERDICT={verdict}"\nexit {gate_rc}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    (tmp_path / "scripts").mkdir()

    script = (
        "set -euo pipefail\n"
        f'ROOT="{tmp_path}"\n'
        f'ALGOVAULT_PY="{stub}"\n'
        + _seam_block().replace(
            '[ -x "$ROOT/.venv/bin/python" ] && SEAM_PY="$ROOT/.venv/bin/python"', "true"
        )
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    combined = proc.stdout + proc.stderr

    assert proc.returncode == want_rc, f"rc={proc.returncode}\n{combined}"
    assert want_in_output in combined, combined
    # and the verdict is always ECHOED — a gate that decides in silence cannot be debugged
    assert verdict in combined or "INDETERMINATE" in combined
