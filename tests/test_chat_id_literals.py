"""OPS-BOT-CHATID-REDACT-W1 — wire the chat-id literal gate into the suite AND the hook.

`scripts/check-chat-id-literals.py` is the real gate (verdict token, own two-way self-test). These
tests make it run on every `pytest`, so CI fails on an id-shaped literal anywhere in the tracked
tree, and they pin the `hooks/pre-push` block that runs it over what a push PUBLISHES.

Precedents: `tests/test_quota_refusal_seam.py` (gate wiring) and `tests/test_pre_push_hook.py` (a
hook block read out of the committed file, never restated — a copy would drift, and the test would
then verify a hook nobody runs).

No fixture id in this file is written as a literal. They are built at runtime, so the gate this
file enforces never has to exempt the file that enforces it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "check-chat-id-literals.py"
HOOK = ROOT / "hooks" / "pre-push"
TOKEN = "CHAT_ID_LITERALS_VERDICT="
FAKE_ID = str(6 * 10**9 + 8_421)  # id-shaped at runtime, never in the source
MASKED = "…" + FAKE_ID[-4:]

# Inside a git hook these point at the REAL repository; a fixture commit must never land there.
_GIT_ENV = {
    k: v for k, v in os.environ.items()
    if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_QUARANTINE_PATH", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
}
_IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


def _run(*args: str, stdin: str | None = None, gate: Path = GATE) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(gate), *args], capture_output=True, text=True, input=stdin
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_IDENT, *args], cwd=repo, env=_GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway repo holding a COPY of the gate at the same relative path, so the gate's own
    `REPO = <script>/../..` resolves to the fixture. That drives the real CLI — argv, stdin, exit
    code, token — which the self-test's direct calls into `run()` structurally cannot reach."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    gate = repo / "scripts" / GATE.name
    shutil.copy2(GATE, gate)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "--", f"scripts/{GATE.name}")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, gate


# ── the gate, on the real tree ───────────────────────────────────────────────────────────────
def test_gate_passes_on_the_current_tree() -> None:
    """THE enforcement: an id-shaped literal anywhere in the tracked tree fails the suite.

    The corpus is git's tracked set, so outside a git checkout (the host's deployed copy, where
    this suite is sometimes run) the gate is honestly INDETERMINATE and this test SKIPS — visibly,
    and never under CI, where a missing checkout would otherwise be laundered into a pass.
    """
    in_git = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT, capture_output=True
    ).returncode == 0
    if not in_git and os.environ.get("CI") != "true":
        pytest.skip("not a git checkout: the gate's corpus is `git ls-files` (the host copy?)")
    r = _run()
    assert f"{TOKEN}PASS" in r.stdout, r.stdout + r.stderr
    assert r.returncode == 0


def test_gate_self_test_passes_both_ways() -> None:
    """The gate must prove it can FAIL, not merely that it can pass."""
    r = _run("--self-test")
    assert "SELF-TEST: PASS" in r.stdout, r.stdout + r.stderr
    assert f"{TOKEN}PASS" in r.stdout
    assert r.returncode == 0


def test_gate_emits_exactly_one_verdict_token() -> None:
    """Callers gate on the TOKEN, so two tokens is as bad as none."""
    for args in ((), ("--self-test",), ("--push-range",)):
        r = _run(*args, stdin="")
        assert r.stdout.count(TOKEN) == 1, (args, r.stdout)


# ── the real CLI, end to end, on a fixture repo ──────────────────────────────────────────────
def test_cli_tree_mode_names_the_file_and_never_prints_the_literal(tmp_path: Path) -> None:
    repo, gate = _fixture_repo(tmp_path)
    (repo / "evidence.py").write_text(f"# chat {FAKE_ID} was shown Starter\n", encoding="utf-8")
    _git(repo, "add", "evidence.py")
    r = _run(gate=gate)
    assert f"{TOKEN}FAIL" in r.stdout and r.returncode == 1, r.stdout + r.stderr
    assert f"evidence.py:1: {MASKED}" in r.stdout
    assert FAKE_ID not in r.stdout + r.stderr, "the gate must never republish what it refused"


def test_cli_push_range_refuses_an_id_that_lives_only_in_a_commit_message(tmp_path: Path) -> None:
    """The vector a tree scan cannot see — and the one this repo's history measurably used."""
    repo, gate = _fixture_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    (repo / "notes.md").write_text("clean content\n", encoding="utf-8")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-q", "-m", f"fix(tier): chat {FAKE_ID} was shown the wrong plan")
    tip = _git(repo, "rev-parse", "HEAD")

    assert _run(gate=gate).returncode == 0, "the TREE is clean — only the message carries it"
    r = _run("--push-range", stdin=f"refs/heads/main {tip} refs/heads/main {base}\n", gate=gate)
    assert f"{TOKEN}FAIL" in r.stdout and r.returncode == 1, r.stdout + r.stderr
    assert f"{tip[:8]} (message):1: {MASKED}" in r.stdout
    assert "does not help" in r.stdout, "a push FAIL must say why a follow-up commit is useless"
    assert FAKE_ID not in r.stdout + r.stderr


def test_cli_push_range_with_nothing_to_publish_is_a_REPORTED_pass(tmp_path: Path) -> None:
    """git runs pre-push on "Everything up-to-date" too; an empty range is a fact, said aloud."""
    _, gate = _fixture_repo(tmp_path)
    r = _run("--push-range", stdin="", gate=gate)
    assert f"{TOKEN}PASS" in r.stdout and r.returncode == 0, r.stdout + r.stderr
    assert "nothing to publish" in r.stdout


def test_cli_push_range_refuses_to_guess_at_unparseable_stdin(tmp_path: Path) -> None:
    _, gate = _fixture_repo(tmp_path)
    r = _run("--push-range", stdin="this is not a pre-push line\n", gate=gate)
    assert f"{TOKEN}INDETERMINATE" in r.stdout and r.returncode == 3, r.stdout + r.stderr


# ── the hook block ───────────────────────────────────────────────────────────────────────────
_VENV_LINE = '[ -x "$ROOT/.venv/bin/python" ] && IDLIT_PY="$ROOT/.venv/bin/python"'


def _idlit_block() -> str:
    """The hook's chat-id block, from `IDLIT_PY=` to the end of its `case`. Read, never restated."""
    src = HOOK.read_text(encoding="utf-8")
    start = src.index('IDLIT_PY="')
    end = src.index("esac", start) + len("esac")
    return src[start:end]


def test_the_refs_are_captured_before_the_first_block_can_swallow_them() -> None:
    """git writes the ref lines to stdin ONCE. If any earlier block's child read them first, the
    gate would scan an empty range and PASS — so the capture must precede every block."""
    src = HOOK.read_text(encoding="utf-8")
    capture = src.index('PUSH_REFS="$(cat)"')
    assert capture < src.index('bash "$ROOT/scripts/lint.sh"')
    assert capture < src.index('SEAM_OUT="$(')
    assert capture < src.index('ORPHAN_OUT="$(')
    assert '"$PUSH_REFS" | "$IDLIT_PY"' in _idlit_block(), "the gate must be FED the captured refs"


def test_both_captures_tolerate_a_gate_that_prints_nothing() -> None:
    """`|| true` on the OUTPUT capture AND on the VERDICT capture. Without the second, a gate that
    prints no token makes grep exit 1 under pipefail and the hook dies silently at that line."""
    assert _idlit_block().count("|| true)") == 2


@pytest.mark.parametrize(
    ("stub_out", "stub_rc", "want_rc", "want_in_output"),
    [
        ("CHAT_ID_LITERALS_VERDICT=PASS", 0, 0, "CHAT_ID_LITERALS_VERDICT=PASS"),
        ("CHAT_ID_LITERALS_VERDICT=FAIL", 1, 1, "push refused"),
        ("CHAT_ID_LITERALS_VERDICT=INDETERMINATE", 3, 0, "verified nothing"),
        # a crash that prints NO token — the case `|| true` on the verdict capture exists for
        ("Traceback (most recent call last): the interpreter died", 1, 0, "verified nothing"),
    ],
)
def test_every_verdict_reaches_the_case_and_the_gate_receives_the_refs(
    tmp_path: Path, stub_out: str, stub_rc: int, want_rc: int, want_in_output: str
) -> None:
    seen = tmp_path / "stdin_seen"
    stub = tmp_path / "stub_gate.sh"
    stub.write_text(
        f'#!/bin/sh\ncat > "{seen}"\necho "{stub_out}"\nexit {stub_rc}\n', encoding="utf-8"
    )
    stub.chmod(0o755)
    refs = f"refs/heads/main {'a' * 40} refs/heads/main {'b' * 40}"

    block = _idlit_block()
    assert _VENV_LINE in block, "the venv override moved; this test would silently stop stubbing"
    script = (
        "set -euo pipefail\n"
        f'ROOT="{tmp_path}"\n'
        f'ALGOVAULT_PY="{stub}"\n'
        f"PUSH_REFS='{refs}'\n" + block.replace(_VENV_LINE, "true")
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    combined = proc.stdout + proc.stderr

    assert proc.returncode == want_rc, f"rc={proc.returncode}\n{combined}"
    assert want_in_output in combined, combined
    assert seen.read_text(encoding="utf-8").strip() == refs, "the gate never saw the push refs"
