#!/usr/bin/env python3
"""OPS-BOT-CHATID-REDACT-W1 — a real chat id never reaches this PUBLIC repo again.

WHY THIS EXISTS (read before "simplifying" it)

This repository is PUBLIC. Incident waves reproduced live defects against the live database and
wrote down what they measured — including the chat id of the subscriber each defect hit: in
comments and docstrings as evidence, as test-fixture VALUES, in test FUNCTION NAMES, in docs, and
in commit MESSAGES. Every one of them was written by a wave doing what it was told (record the
evidence), and nothing ever asked whether the evidence was publishable.

Redacting them retires the INSTANCES. This retires the CLASS:
  - an id-shaped literal anywhere in the tracked tree fails `pytest` (CI and local), and
  - one in anything a push is about to PUBLISH — the ADDED lines and the MESSAGE of every
    commit in the range — refuses the push in `hooks/pre-push`,
by file and line, with the literal MASKED.

THE RULE, AND WHY IT IS NOT "NEAR THE WORD chat"
────────────────────────────────────────────────
Any bare run of 8–16 digits is presumed to be a pasted identifier. The ids this wave redacted are
9 and 10 digits (new Telegram ids are 10); 16 is the ceiling of a 52-bit Telegram id, and it
covers a `-100…` supergroup id (13). Keyword proximity was considered and REJECTED on measurement:
a real id sat in `args = (<id>, "ETH", "15m", "BINANCE")`, a tuple with no keyword anywhere near
it, and another inside a test FUNCTION NAME.

Three shapes are NOT flagged. Each was measured in this tree on the day the gate shipped, and there
is no fourth and no value list, because an exemption guarding a class nobody writes is negative
value (`verification-gates.md`):
  1. a FRACTIONAL part (`-0.000011994`) — funding rates in the alert-image fixtures;
  2. DIGIT-GROUPED literals (`1_700_002_800`, `985_000_000`) — epochs and volumes. No group reaches
     8 digits, so none is a candidate. A magnitude a human TYPES carries groups; an id PASTED from
     a DB row or a log never does;
  3. a digit run INSIDE an alphanumeric token — a hex digest, the base-62 alphabet string. (An
     underscore is NOT alphanumeric here: `test_the_<id>_shape` is flagged.)

WHAT IT DOES NOT CATCH — stated so it is never trusted for work it cannot do
───────────────────────────────────────────────────────────────────────────
  - an id of 7 digits or fewer (the oldest Telegram accounts);
  - an id written with digit groups, or glued to letters (`chat1234…`);
  - an id encoded (hex, base64) or HASHED — and a SHA-256 of a 10-digit id is NOT a redaction,
    a 10^10 space brute-forces in minutes;
  - anything ALREADY in public history. This gate guards what is published NEXT;
  - a push from a clone without `core.hooksPath hooks`, or a web-UI edit. CI's tree scan still
    fires on the TREE, after the fact — nothing outside the hook ever reads a commit MESSAGE.
A LEGITIMATE bare 8–16-digit literal (a compact date, a UUID's last group) fails too. That is the
intended direction: an unregistered case fails toward NOISE, never toward silence. Write the
magnitude with digit groups, or add a reasoned exemption HERE — never a heuristic elsewhere.

CONTRACT
────────
Prints exactly one terminal `CHAT_ID_LITERALS_VERDICT=PASS|FAIL|INDETERMINATE`. Callers gate on the
TOKEN, never the exit code. Exit 0=PASS, 1=FAIL, 3=INDETERMINATE (the token-law default for a NEW
gate, as in `check-orphan-modules.py`). A finding prints as `…<last4>` and NEVER in full: this
repo's CI logs are public, and a gate that republishes the literal it refused is the leak again.

Modes
  (none)         every TRACKED file of the working tree — what `tests/test_chat_id_literals.py`
                 runs, so CI and local `pytest` enforce it. Not a git checkout → INDETERMINATE.
  --push-range   the pre-push stdin (`<local ref> <local sha> <remote ref> <remote sha>` lines).
                 Every commit the push would publish: its ADDED lines and its MESSAGE. A literal
                 added in one commit and deleted in the next is still FAIL — the history publishes
                 it even though the final tree is clean. Empty stdin is a fact (git runs the hook
                 on "Everything up-to-date" too) → PASS, said out loud. Unparseable → INDETERMINATE.
  --self-test    two-way and vacuity-guarded. Its fixture ids are BUILT AT RUNTIME, so this file
                 never contains the shape it forbids and needs no exemption for itself.

This module imports nothing from `src/`, deliberately — same reason as `check-orphan-modules.py`.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import NamedTuple

REPO = pathlib.Path(__file__).resolve().parent.parent

VERDICT_PASS = "CHAT_ID_LITERALS_VERDICT=PASS"
VERDICT_FAIL = "CHAT_ID_LITERALS_VERDICT=FAIL"
VERDICT_INDETERMINATE = "CHAT_ID_LITERALS_VERDICT=INDETERMINATE"

MIN_DIGITS = 8
MAX_DIGITS = 16

# A bare run of MIN..MAX digits, bounded on both sides by something that is neither a digit nor an
# ASCII letter (exemption 3), not the fractional part of a decimal (exemption 1, lookbehind), and
# not the integer part of one. Underscores are deliberately NOT in the boundary class, so a digit
# GROUP never reaches the floor (exemption 2) while `_<id>_` inside a name is still a match.
ID_SHAPED = re.compile(
    rb"(?<![0-9A-Za-z])(?<![0-9]\.)[0-9]{%d,%d}(?![0-9A-Za-z])(?!\.[0-9])" % (MIN_DIGITS, MAX_DIGITS)
)
ZERO_SHA = re.compile(r"^0+$")
SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
BINARY_SNIFF = 8192

REMEDY = (
    "An 8-16-digit literal reads as a pasted identifier (a Telegram chat id, a phone number), "
    "and this repo is PUBLIC.",
    "  - a chat id: use a synthetic id of <= 7 digits that keeps its last4 (16212 for last4 "
    "6212), or write `chat last4 NNNN` in prose;",
    "  - a genuine magnitude (an epoch, a volume): write it with digit groups, 1_700_000_000.",
)
REMEDY_PUSH = (
    "  - it is in a commit being PUSHED: deleting it in a NEW commit does not help, because the "
    "history publishes it. Rewrite the unpushed commit(s) (`git commit --amend`, or reword/edit "
    "them in a rebase onto the remote branch) and push again.",
)


class Finding(NamedTuple):
    where: str
    last4: str


class Indeterminate(Exception):
    """Input we were handed and could not read. Never a silent pass."""


def mask(digits: bytes | str) -> str:
    s = digits.decode() if isinstance(digits, bytes) else digits
    return "…" + s[-4:]


def scan_bytes(data: bytes, label: str) -> list[Finding]:
    """Every id-shaped literal in ``data``, located as ``label:<line>``. Pure."""
    out: list[Finding] = []
    for m in ID_SHAPED.finditer(data):
        line = data.count(b"\n", 0, m.start()) + 1
        out.append(Finding(f"{label}:{line}", mask(m.group())))
    return out


def _git(args: list[str], cwd: pathlib.Path, env: dict[str, str] | None = None) -> bytes:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, env=env, check=False
    )
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip().splitlines()
        raise Indeterminate(f"`git {' '.join(args[:3])}…` failed: {err[-1] if err else proc.returncode}")
    return proc.stdout


# ── mode 1: the tracked tree ─────────────────────────────────────────────────────────────────
def scan_tree(repo: pathlib.Path) -> tuple[list[Finding], list[str]]:
    """Every tracked file, as it is in the working tree. Returns (findings, notes)."""
    try:
        listing = _git(["ls-files", "-z"], repo)
    except (Indeterminate, OSError) as exc:
        raise Indeterminate(f"cannot list tracked files under {repo}: {exc}") from exc
    paths = [p.decode() for p in listing.split(b"\0") if p]
    if not paths:
        # WE construct this corpus, so an empty one means we built nothing — vacuity, not a pass.
        raise Indeterminate(f"`git ls-files` returned no tracked files under {repo}")

    findings: list[Finding] = []
    scanned = binary = missing = 0
    for rel in paths:
        p = repo / rel
        if p.is_symlink():
            data = os.readlink(p).encode()  # git publishes the link TEXT, not the target
        elif p.is_file():
            data = p.read_bytes()
        else:
            missing += 1  # tracked but deleted in the working tree: nothing on disk to scan
            continue
        if b"\0" in data[:BINARY_SNIFF]:
            binary += 1
            continue
        scanned += 1
        findings.extend(scan_bytes(data, rel))
    notes = [f"tree: {scanned} tracked text files scanned ({binary} binary, {missing} absent)"]
    if scanned == 0:
        raise Indeterminate(f"{len(paths)} tracked paths but none was a readable text file")
    return findings, notes


# ── mode 2: what a push publishes ────────────────────────────────────────────────────────────
def parse_push_stdin(text: str) -> list[tuple[str, str, str, str]]:
    refs: list[tuple[str, str, str, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 4 or not SHA.match(fields[1]) or not SHA.match(fields[3]):
            raise Indeterminate(f"pre-push stdin line {n} is not `<ref> <sha> <ref> <sha>`")
        refs.append((fields[0], fields[1], fields[2], fields[3]))
    return refs


def commits_to_publish(repo: pathlib.Path, refs: list[tuple[str, str, str, str]]) -> list[str]:
    """The commits these ref updates make public, oldest first, de-duplicated across refs."""
    seen: set[str] = set()
    ordered: list[str] = []
    for _local_ref, local_sha, _remote_ref, remote_sha in refs:
        if ZERO_SHA.match(local_sha):
            continue  # a deletion publishes nothing
        known_remote = not ZERO_SHA.match(remote_sha) and subprocess.run(
            ["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd=repo, capture_output=True
        ).returncode == 0
        if known_remote:
            rng = ["rev-list", "--reverse", f"{remote_sha}..{local_sha}"]
        else:
            # A new branch, or a remote tip we have not fetched: everything not already on a
            # remote-tracking ref. Over-scans a stale clone, which is the safe direction.
            rng = ["rev-list", "--reverse", local_sha, "--not", "--remotes"]
        for sha in _git(rng, repo).decode().split():
            if sha not in seen:
                seen.add(sha)
                ordered.append(sha)
    return ordered


def scan_commit(repo: pathlib.Path, sha: str) -> list[Finding]:
    short = sha[:8]
    findings = scan_bytes(_git(["log", "-1", "--format=%B", sha], repo), f"{short} (message)")

    diff = _git(
        ["diff-tree", "-p", "--no-color", "--no-ext-diff", "--no-textconv", "-U0", "--root",
         "--diff-merges=first-parent", "--no-commit-id", sha],
        repo,
    )
    path = "?"
    new_line = 0
    in_header = True
    for raw in diff.split(b"\n"):
        if raw.startswith(b"diff --git "):
            in_header = True
            path = "?"
        elif in_header and raw.startswith(b"+++ "):
            target = raw[4:].strip().strip(b'"')
            path = (target[2:] if target.startswith(b"b/") else target).decode(errors="replace")
        elif raw.startswith(b"@@ "):
            m = HUNK.match(raw)
            new_line = int(m.group(1)) if m else 0
            in_header = False
        elif not in_header and raw.startswith(b"+"):
            for f in scan_bytes(raw[1:], f"{short} {path}"):
                findings.append(Finding(f"{short} {path}:{new_line}", f.last4))
            new_line += 1
    return findings


def scan_push(repo: pathlib.Path, stdin_text: str) -> tuple[list[Finding], list[str]]:
    refs = parse_push_stdin(stdin_text)
    if not refs:
        return [], ["push range: no ref lines on stdin — nothing to publish"]
    commits = commits_to_publish(repo, refs)
    findings: list[Finding] = []
    for sha in commits:
        findings.extend(scan_commit(repo, sha))
    return findings, [f"push range: {len(refs)} ref(s), {len(commits)} commit(s) scanned "
                      "(added lines + messages)"]


# ── verdict ──────────────────────────────────────────────────────────────────────────────────
def emit(verdict: str, findings: list[Finding], notes: list[str], *, push: bool = False) -> int:
    for n in notes:
        print(f"[chat-id-literals] {n}")
    if verdict == VERDICT_FAIL:
        print(f"[chat-id-literals] {len(findings)} id-shaped literal(s), masked:")
        for f in findings:
            print(f"[chat-id-literals]   {f.where}: {f.last4}")
        for line in REMEDY + (REMEDY_PUSH if push else ()):
            print(f"[chat-id-literals] {line}")
    print(verdict)
    return {VERDICT_PASS: 0, VERDICT_FAIL: 1}.get(verdict, 3)


def run(mode: str, repo: pathlib.Path, stdin_text: str = "") -> tuple[str, list[Finding], list[str]]:
    """(verdict, findings, notes). Never raises: a crash is a verdict, not a traceback."""
    try:
        if mode == "push":
            findings, notes = scan_push(repo, stdin_text)
        else:
            findings, notes = scan_tree(repo)
    except Indeterminate as exc:
        return VERDICT_INDETERMINATE, [], [str(exc)]
    except Exception as exc:  # noqa: BLE001 — an unexpected crash must still emit a verdict
        return VERDICT_INDETERMINATE, [], [f"unexpected: {type(exc).__name__}: {exc}"]
    return (VERDICT_FAIL if findings else VERDICT_PASS), findings, notes


# ── self-test ────────────────────────────────────────────────────────────────────────────────
# Fixture ids are ARITHMETIC, so no id-shaped run is ever written into this file.
_ID10 = str(7 * 10**9 + 4_521)          # a 10-digit id
_ID8 = str(3 * 10**7 + 6_212)           # the 8-digit floor
_SUPERGROUP = "-100" + str(2 * 10**9 + 9)  # a 13-digit `-100…` supergroup id
_SEVEN = str(10**6 + 234)               # 7 digits: below the floor
_SEVENTEEN = str(10**16 + 5)            # 17 digits: above a 52-bit Telegram id
_FIXTURE_ENV = {
    k: v for k, v in os.environ.items()
    if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_QUARANTINE_PATH", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
}
_GIT_ID = ["-c", "user.name=self-test", "-c", "user.email=self-test@example.invalid",
           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


def _fx_git(repo: pathlib.Path, *args: str) -> str:
    """git on a FIXTURE repo, with the hook-exported GIT_* vars stripped: inside a hook they
    point at the REAL repository, and a fixture commit must never land there."""
    return _git([*_GIT_ID, *args], repo, env=_FIXTURE_ENV).decode().strip()


def _fx_repo(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    root.mkdir(parents=True)
    _fx_git(root, "init", "-q", "-b", "main")
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
        _fx_git(root, "add", "--", rel)
    _fx_git(root, "commit", "-q", "--allow-empty", "-m", "base")
    return root


def _fx_commit(repo: pathlib.Path, rel: str, body: str, message: str) -> str:
    (repo / rel).write_text(body, encoding="utf-8")
    _fx_git(repo, "add", "--", rel)
    _fx_git(repo, "commit", "-q", "-m", message)
    return _fx_git(repo, "rev-parse", "HEAD")


def self_test() -> int:
    import contextlib
    import io

    results: list[tuple[str, bool, str]] = []

    def check(label: str, got: object, want: object) -> None:
        results.append((label, got == want, f"got {got!r}, want {want!r}"))

    def fires(text: str) -> int:
        return len(scan_bytes(text.encode(), "fx"))

    # (a) MUST FIRE — every shape a real id actually took in this repo, plus the range ends.
    must_fire = {
        "comment": f"# chat {_ID10} was shown Starter\n",
        "docstring": f'"""Live chat {_ID10}, 2026-09-06."""\n',
        "code literal": f'db.upsert_subscriber({_ID10}, "subject", "en")\n',
        "tuple, no keyword": f'args = ({_ID10}, "ETH", "15m", "BINANCE")\n',
        "function name": f"def test_the_{_ID10}_shape_renders_Pro():\n",
        "id then _digit": f"key_{_ID10}_2\n",
        "query string": f"curl 'http://127.0.0.1:3000/api/referral/code?tg={_ID10}'\n",
        "markdown": f"(`{_ID10}`, peak 248)\n",
        "8-digit floor": f"chat {_ID8}\n",
        "supergroup": f"CHANNEL = {_SUPERGROUP}\n",
        "end of sentence": f"the chat was {_ID10}.\n",
    }
    for label, text in must_fire.items():
        check(f"(a) fires: {label}", fires(text), 1)

    # (b) MUST NOT FIRE — the three measured exemptions, the range ends, and the redacted forms.
    must_not_fire = {
        "fractional part": "funding_rate=-0.000011994,\n",
        "digit-grouped epoch": "now = 1_700_002_800\n",
        "digit-grouped volume": "volume_24h=10_065_514_788.95\n",
        "hex digest": "# 0c4004b7726a78b1bca" + str(10**9 + 365) + "b1a630c0c652be302dc\n",
        # digits FIRST, so only the lookAHEAD can exempt it (the case above is also held by the
        # lookbehind, which is how a lookahead-only mutation survived the first battery)
        "hex digest, digits first": "sha " + str(10**9 + 365) + "b1a630c0c652be302dc\n",
        "alphabet string": '"abcdefghijklmnopqrstuvwxyz0123456789_-"\n',
        "7 digits": f"chat {_SEVEN}\n",
        "17 digits": f"n = {_SEVENTEEN}\n",
        "float integer part": "v = " + str(10**8 + 7) + ".5\n",
        "redacted prose": "chat last4 6212 upgraded; chat …6212\n",
        "synthetic stand-in": "tmp_db.upsert_subscriber(16212, 'subject', 'en')\n",
    }
    for label, text in must_not_fire.items():
        check(f"(b) silent: {label}", fires(text), 0)

    # (c) the finding is MASKED and located.
    got = scan_bytes(f"a\nb {_ID10}\n".encode(), "f.py")
    check("(c) located on its line", [f.where for f in got], ["f.py:2"])
    check("(c) masked to last4", [f.last4 for f in got], ["…" + _ID10[-4:]])

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)

        # (d) TREE mode, both ways, and the printed output never carries the full literal.
        dirty = _fx_repo(tmp / "dirty", {"src/x.py": f"# chat {_ID10}\n", "README.md": "ok\n"})
        v, f, _ = run("tree", dirty)
        check("(d) tree with an id → FAIL", v, VERDICT_FAIL)
        check("(d) tree names the file", [x.where for x in f], ["src/x.py:1"])
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            emit(v, f, [])
        check("(d) FAIL output never prints the full literal", _ID10 in buf.getvalue(), False)
        check("(d) FAIL output prints the masked form", ("…" + _ID10[-4:]) in buf.getvalue(), True)
        clean = _fx_repo(tmp / "clean", {"src/x.py": "# chat last4 4521\n"})
        check("(d) clean tree → PASS", run("tree", clean)[0], VERDICT_PASS)

        # (e) TREE mode vacuity: WE construct the corpus, so building nothing is INDETERMINATE.
        empty = _fx_repo(tmp / "empty", {})
        check("(e) no tracked files → INDETERMINATE", run("tree", empty)[0], VERDICT_INDETERMINATE)
        plain = tmp / "not-a-repo"
        plain.mkdir()
        check("(e) not a git checkout → INDETERMINATE", run("tree", plain)[0], VERDICT_INDETERMINATE)

        # (f) PUSH mode — the corpus a tree scan structurally cannot see.
        r = _fx_repo(tmp / "push", {"src/a.py": "x = 1\n"})
        base = _fx_git(r, "rev-parse", "HEAD")
        _fx_git(r, "update-ref", "refs/remotes/origin/main", base)  # "already published"

        def push(tip: str, remote: str = base) -> str:
            return run("push", r, f"refs/heads/main {tip} refs/heads/main {remote}\n")[0]

        tip = _fx_commit(r, "src/a.py", f"x = {_ID10}\n", "add a value")
        check("(f) id in an ADDED line → FAIL", push(tip), VERDICT_FAIL)
        _fx_git(r, "reset", "-q", "--hard", base)
        tip = _fx_commit(r, "src/a.py", "x = 2\n", f"fix: chat {_ID10} saw the wrong tier")
        check("(f) id only in the commit MESSAGE → FAIL", push(tip), VERDICT_FAIL)
        _fx_git(r, "reset", "-q", "--hard", base)
        _fx_commit(r, "src/a.py", f"x = {_ID10}\n", "add")
        tip = _fx_commit(r, "src/a.py", "x = 3\n", "remove again")
        check("(f) added then deleted inside the range → FAIL (history publishes it)",
              push(tip), VERDICT_FAIL)
        check("(f) ...though the final TREE is clean", run("tree", r)[0], VERDICT_PASS)
        # a deletion of an id that was ALREADY public publishes nothing new
        _fx_git(r, "update-ref", "refs/remotes/origin/main", tip)
        prior = _fx_commit(r, "src/a.py", f"x = {_ID10}\n", "reintroduce")
        _fx_git(r, "update-ref", "refs/remotes/origin/main", prior)  # pretend it went out earlier
        tip = _fx_commit(r, "src/a.py", "x = 4\n", "redact")
        check("(f) a REMOVAL-only commit → PASS", push(tip, prior), VERDICT_PASS)
        check("(f) a new branch (remote sha 0) uses --not --remotes → PASS",
              run("push", r, f"refs/heads/b {tip} refs/heads/b {'0' * 40}\n")[0], VERDICT_PASS)
        dirty_tip = _fx_commit(r, "src/a.py", f"x = {_ID10}\n", "on a new branch")
        check("(f) ...and a new branch carrying an id → FAIL",
              run("push", r, f"refs/heads/b {dirty_tip} refs/heads/b {'0' * 40}\n")[0],
              VERDICT_FAIL)
        _fx_git(r, "reset", "-q", "--hard", tip)
        check("(f) a ref DELETION publishes nothing → PASS",
              run("push", r, f"(delete) {'0' * 40} refs/heads/b {tip}\n")[0], VERDICT_PASS)
        check("(f) empty stdin (up-to-date push) → PASS", run("push", r, "")[0], VERDICT_PASS)
        check("(f) unparseable stdin → INDETERMINATE",
              run("push", r, "not four fields\n")[0], VERDICT_INDETERMINATE)
        check("(f) a sha git cannot resolve → INDETERMINATE",
              run("push", r, f"refs/heads/main {'e' * 40} refs/heads/main {'0' * 40}\n")[0],
              VERDICT_INDETERMINATE)

        # (g) THE SEAM THE FIXTURES BYPASS: the real tree must be READABLE by the real corpus
        # builder. A hermetic self-test is blind to exactly what its fixtures replace.
        v_real, _, notes_real = run("tree", REPO)
        in_git = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=REPO,
                                capture_output=True).returncode == 0
        if in_git:
            check("(g) the real tree is not INDETERMINATE", v_real != VERDICT_INDETERMINATE, True)
            scanned = [int(m.group(1)) for n in notes_real
                       if (m := re.match(r"tree: (\d+) tracked text files scanned", n))]
            check("(g) the real corpus is non-empty", bool(scanned) and scanned[0] > 0, True)

    # (h) the TOKEN→EXIT-CODE mapping itself, and exactly one token per run. The labels name the
    # verdict WITHOUT the token prefix: a label carrying the full token would put a second one in
    # this run's own output, the exact ambiguity the one-token rule forbids.
    for verdict, want in ((VERDICT_PASS, 0), (VERDICT_FAIL, 1), (VERDICT_INDETERMINATE, 3)):
        name = verdict.split("=", 1)[1]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            got_rc = emit(verdict, [Finding("x:1", "…0000")] if verdict == VERDICT_FAIL else [], [])
        check(f"(h) {name} -> exit {want}", got_rc, want)
        check(f"(h) {name} token printed once", buf.getvalue().count("CHAT_ID_LITERALS_VERDICT="), 1)

    must_fire_n = sum(1 for lbl, _, _ in results if lbl.startswith("(a)"))
    must_not_n = sum(1 for lbl, _, _ in results if lbl.startswith("(b)"))
    failed = [(lbl, why) for lbl, ok, why in results if not ok]
    for lbl, ok, why in results:
        print(f"[self-test] {'PASS' if ok else 'FAIL'} {lbl}" + ("" if ok else f" — {why}"))
    if must_fire_n == 0 or must_not_n == 0:
        print(f"[self-test] VACUOUS: {must_fire_n} must-fire, {must_not_n} must-not-fire")
        print(VERDICT_INDETERMINATE)
        return 3
    print(f"SELF-TEST: {'PASS' if not failed else 'FAIL'} ({len(results) - len(failed)} passed, "
          f"{len(failed)} failed; {must_fire_n} must-fire, {must_not_n} must-not-fire)")
    print(VERDICT_FAIL if failed else VERDICT_PASS)
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        try:
            return self_test()
        except Exception as exc:  # noqa: BLE001 — a crashing self-test is a verdict too
            print(f"[self-test] crashed: {type(exc).__name__}: {exc}")
            print(VERDICT_INDETERMINATE)
            return 3
    if "--push-range" in argv:
        stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""
        verdict, findings, notes = run("push", REPO, stdin_text)
        return emit(verdict, findings, notes, push=True)
    verdict, findings, notes = run("tree", REPO)
    return emit(verdict, findings, notes)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
