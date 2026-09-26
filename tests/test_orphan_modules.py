"""OPS-BOT-PREPUSH-NOTOKEN-ORPHAN-CI-W1 — the orphan-module gate runs in CI, not only on push.

`scripts/check-orphan-modules.py` (OPS-BOT-DEAD-SURFACE-SWEEP-W1 CH3 R8) says a dead module
"fails CI and `hooks/pre-push`", and the vault card says "pre-push + CI". Measured at `ffd419e`,
half of that was false: `.github/workflows/test.yml` runs ruff, mypy and pytest, and no test
invoked the gate (one comment in `tests/test_dead_schema_has_no_readers.py` names it). So a push
without the hook — a clone missing `core.hooksPath hooks`, a web edit, a merge — shipped a dead
module green. These tests put the gate in every `pytest` run, which is what CI runs.

Precedent: `tests/test_quota_refusal_seam.py` (current tree PASS · self-test PASS both ways ·
exactly one verdict token).

Verdicts are read the way `hooks/pre-push` reads them — LINE-anchored — never by substring. A
self-test label that quotes a token would otherwise satisfy a `"…=PASS" in stdout` check while
the self-test itself failed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "check-orphan-modules.py"
TOKEN = "ORPHAN_MODULES_VERDICT="


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True)


def _verdicts(stdout: str) -> list[str]:
    """Every token LINE, in order — what the hook's `grep -oE '^…=[A-Z]+'` reads."""
    return re.findall(rf"^{TOKEN}([A-Z]+)$", stdout, re.M)


def test_gate_passes_on_the_current_tree() -> None:
    """THE enforcement: a module no declared entrypoint reaches fails the suite, by name."""
    r = _run()
    assert _verdicts(r.stdout) == ["PASS"], r.stdout + r.stderr
    assert r.returncode == 0
    # a REPORTED pass: the corpus it walked is printed, and it is not empty
    m = re.search(r"^\[orphan-modules\] (\d+) modules, \d+ roots, (\d+) reachable$", r.stdout, re.M)
    assert m and int(m.group(1)) > 0 and m.group(1) == m.group(2), r.stdout


def test_gate_self_test_passes_both_ways() -> None:
    """The gate must prove it can FAIL, not merely that it can pass."""
    r = _run("--self-test")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _verdicts(r.stdout) == ["PASS"], r.stdout
    m = re.search(r"^\[self-test\] (\d+)/(\d+) passed$", r.stdout, re.M)
    assert m and int(m.group(2)) > 0 and m.group(1) == m.group(2), r.stdout
    # both ways: a scenario that must FAIL and one that must PASS each ran and held
    assert re.search(r"^\[self-test\] PASS .* → FAIL$", r.stdout, re.M), r.stdout
    assert re.search(r"^\[self-test\] PASS .* → PASS$", r.stdout, re.M), r.stdout


def test_gate_emits_exactly_one_verdict_token() -> None:
    """Callers gate on the TOKEN, so two tokens is as bad as none — in every mode."""
    for args in ((), ("--self-test",)):
        r = _run(*args)
        assert r.stdout.count(TOKEN) == 1, (args, r.stdout)
