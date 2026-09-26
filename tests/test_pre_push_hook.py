"""The pre-push hook reaches its verdict `case` for all three tokens — and when there is NO token.

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

OPS-BOT-PREPUSH-NOTOKEN-ORPHAN-CI-W1 — THE SAME DEFECT, ONE LINE LOWER. R4 put `|| true` on the
OUTPUT capture only. The VERDICT capture beneath it (`… | grep -oE '^…_VERDICT=[A-Z]+' | tail -1`)
still killed the hook whenever a gate printed NO token at all — an interpreter crash, an import
error, a missing python: grep exits 1, pipefail fails the pipeline, `set -e` aborts at that
assignment, exit 1, no message. The orphan block copied the shape. R4's own pin could not see
it: `"|| true)" in _seam_block()` was satisfied by the OUTPUT capture — a substring satisfied
elsewhere in the artifact.

So every gate block is now ENUMERATED from the hook, never hand-listed, and driven through the
three tokens and three no-token failures. A new block that copies the unsafe shape fails here on
the day it lands, with no edit to this file. Honest scope: a block is found by its
`<NAME>_PY="${ALGOVAULT_PY:-python3}"` line, and a `<X>_VERDICT=` capture outside every found
block fails the suite (noise, never silence); a verdict read in some wholly different shape is
not seen.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "pre-push"

_BLOCK_START = re.compile(r'^([A-Z]+)_PY="\$\{ALGOVAULT_PY:-python3\}"$', re.M)
_VERDICT_CAPTURE = re.compile(r'^[A-Z_]+_VERDICT="\$\(', re.M)
_KNOWN_BLOCKS = {"SEAM", "ORPHAN", "IDLIT"}


def _seam_block() -> str:
    """The hook's seam block, from `SEAM_PY=` to the end of its `case`. Read, never restated."""
    src = HOOK.read_text(encoding="utf-8")
    start = src.index('SEAM_PY="')
    end = src.index("esac", start) + len("esac")
    return src[start:end]


def _block_spans() -> dict[str, tuple[int, int]]:
    """Every gate block in the hook, `<NAME>_PY=` through the end of its `case`. Read, never
    restated — and enumerated, never hand-listed."""
    src = HOOK.read_text(encoding="utf-8")
    return {
        m.group(1): (m.start(), src.index("\nesac", m.start()) + len("\nesac"))
        for m in _BLOCK_START.finditer(src)
    }


def _blocks() -> dict[str, str]:
    src = HOOK.read_text(encoding="utf-8")
    return {name: src[start:end] for name, (start, end) in _block_spans().items()}


# Collected once. If the start-line shape ever drifts this is EMPTY, pytest SKIPS every test
# parametrized over it, and only the vacuity test below stands between that and a silent pass.
BLOCKS = sorted(_blocks())


def _token(block: str) -> str:
    m = re.search(r"grep -oE '\^([A-Z_]+_VERDICT)=\[A-Z\]\+'", block)
    assert m, "the block's verdict read moved; this test would stop driving it"
    return m.group(1)


def _drive(tmp_path: Path, name: str, interpreter: str) -> subprocess.CompletedProcess[str]:
    """Run ONE block, verbatim, under the hook's own shell options, with `interpreter` standing
    in for python. A sentinel after the block proves the shell got PAST it."""
    block = _blocks()[name]
    venv_line = f'[ -x "$ROOT/.venv/bin/python" ] && {name}_PY="$ROOT/.venv/bin/python"'
    assert venv_line in block, "the venv override moved; this test would silently stop stubbing"
    script = (
        "set -euo pipefail\n"
        f'ROOT="{tmp_path}"\n'
        f'ALGOVAULT_PY="{interpreter}"\n'
        "PUSH_REFS=''\n"
        + block.replace(venv_line, "true")
        + '\necho "BLOCK_SURVIVED"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def _stub(tmp_path: Path, body: str, rc: int) -> str:
    stub = tmp_path / "stub_gate.sh"
    stub.write_text(f"#!/bin/sh\n{body}\nexit {rc}\n", encoding="utf-8")
    stub.chmod(0o755)
    return str(stub)


def test_the_hook_still_sets_e_so_this_test_is_not_vacuous() -> None:
    """If someone drops `set -euo pipefail`, the hazard is gone and so is this test's subject.

    A vacuity guard at the CONSTRUCTION site: without `set -e` every case below would pass for a
    reason that has nothing to do with the fix, and the suite would report a guard it no longer
    has. Assert the hazard exists before asserting it is handled.
    """
    assert re.search(r"^set -euo pipefail$", HOOK.read_text(encoding="utf-8"), re.M)


def test_the_enumeration_finds_every_known_block_and_no_stray_verdict_read() -> None:
    """The vacuity guard for everything parametrized over `BLOCKS`, at the site that builds it.

    Named, not counted: a drifted start line would otherwise drop a block and read as a smaller,
    greener suite. And a verdict capture that no enumerated block contains is a block this file
    cannot drive — it fails here, loudly, instead of going untested.
    """
    src = HOOK.read_text(encoding="utf-8")
    spans = _block_spans()
    assert _KNOWN_BLOCKS <= set(spans), sorted(spans)
    assert len(_BLOCK_START.findall(src)) == len(spans), "two blocks share a name"
    for name, block in _blocks().items():
        assert len(_VERDICT_CAPTURE.findall(block)) == 1, f"{name}: want exactly one verdict read"
    for m in _VERDICT_CAPTURE.finditer(src):
        line = src[m.start() : src.index("\n", m.start())]
        assert any(s <= m.start() < e for s, e in spans.values()), f"not in any block: {line}"


@pytest.mark.parametrize("name", BLOCKS)
def test_both_captures_tolerate_a_non_zero_or_silent_gate(name: str) -> None:
    """`|| true` on the OUTPUT capture AND on the VERDICT capture, in every block.

    Replaces R4's `"|| true)" in _seam_block()`, which the OUTPUT capture alone satisfied — so it
    stayed green while the VERDICT capture could still kill the hook. Counted, never found.
    """
    assert _blocks()[name].count("|| true)") == 2, (
        "without `|| true` on both captures, the token-reading case is dead code under set -e"
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


@pytest.mark.parametrize("name", BLOCKS)
@pytest.mark.parametrize(
    ("verdict", "gate_rc", "want_rc", "want_on_stderr"),
    [
        pytest.param("PASS", 0, 0, None, id="PASS"),
        pytest.param("FAIL", 1, 1, "push refused", id="FAIL"),
        pytest.param("INDETERMINATE", 3, 0, "verified nothing", id="INDETERMINATE"),
    ],
)
def test_every_block_reaches_its_case_for_every_token(
    tmp_path: Path, name: str, verdict: str, gate_rc: int, want_rc: int, want_on_stderr: str | None
) -> None:
    """R4's matrix, for EVERY enumerated block rather than the seam alone."""
    token = f"{_token(_blocks()[name])}={verdict}"
    proc = _drive(tmp_path, name, _stub(tmp_path, f'echo "{token}"', gate_rc))
    combined = proc.stdout + proc.stderr

    assert proc.returncode == want_rc, f"rc={proc.returncode}\n{combined}"
    assert token in combined, "a gate that decides in silence cannot be debugged"
    if want_on_stderr:
        assert want_on_stderr in proc.stderr, combined
    # FAIL is the only verdict that stops the hook; the other two must let it carry on
    assert ("BLOCK_SURVIVED" in proc.stdout) is (verdict != "FAIL"), combined


@pytest.mark.parametrize("name", BLOCKS)
@pytest.mark.parametrize(
    ("failure", "body", "gate_rc", "evidence"),
    [
        # the interpreter dies before it can print anything but a traceback
        pytest.param(
            "crash",
            'echo "Traceback (most recent call last): ImportError: boom" >&2',
            1,
            "boom",
            id="crash",
        ),
        # exits 0 with no token (a truncated script) — isolates the VERDICT capture, because the
        # OUTPUT capture cannot trip on an exit 0
        pytest.param("silent-exit-0", ":", 0, None, id="silent-exit-0"),
        # no python at all: bash itself reports it, into the captured output
        pytest.param(
            "missing-interpreter", None, None, "No such file or directory", id="missing-interpreter"
        ),
    ],
)
def test_every_block_survives_a_gate_that_prints_no_token(
    tmp_path: Path,
    name: str,
    failure: str,
    body: str | None,
    gate_rc: int | None,
    evidence: str | None,
) -> None:
    """THE fix. No token is INDETERMINATE: it must NOT block, and it must be LOUD — the gate's own
    output replayed and the verdict named. Pre-fix, the seam and orphan blocks exited 1 here,
    printing nothing, and every block after them never ran."""
    if body is None or gate_rc is None:
        interpreter = str(tmp_path / "no-such-python")
    else:
        interpreter = _stub(tmp_path, body, gate_rc)
    proc = _drive(tmp_path, name, interpreter)
    combined = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"{failure}: rc={proc.returncode}\n{combined}"
    assert "BLOCK_SURVIVED" in proc.stdout, f"{failure}: the hook died inside the block\n{combined}"
    assert "emitted no verdict (INDETERMINATE)" in proc.stderr, combined
    assert "verified nothing" in proc.stderr, combined
    if evidence:
        assert evidence in proc.stderr, f"{failure}: the gate's output was swallowed\n{combined}"
