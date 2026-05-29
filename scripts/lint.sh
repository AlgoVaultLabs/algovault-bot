#!/usr/bin/env bash
# OPS-BOT-CI-LINT-TYPECHECK-CLEANUP-W1 — canonical local lint + type check.
# Mirrors .github/workflows/test.yml exactly: `ruff check src tests` + `mypy src`.
# Run manually (`bash scripts/lint.sh`) or automatically via hooks/pre-push.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Prefer the repo venv binaries if present (matches the dev environment);
# fall back to PATH (matches CI, which pip-installs ruff/mypy).
RUFF=ruff
MYPY=mypy
[ -x .venv/bin/ruff ] && RUFF=.venv/bin/ruff
[ -x .venv/bin/mypy ] && MYPY=.venv/bin/mypy

echo "→ $RUFF check src tests"
"$RUFF" check src tests
echo "→ $MYPY src"
"$MYPY" src
echo "✓ lint + type checks passed"
