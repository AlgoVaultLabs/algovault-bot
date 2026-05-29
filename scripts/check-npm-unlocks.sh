#!/usr/bin/env bash
# TG-BROADCAST-STACK-W1 CH6 (2026-05-28): cron wrapper for check-npm-unlocks.py.
# Cadence: every 10 min (per architect Q-C ratification — avoids 5m grid collision).

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/algovault-bot}"
PY="${REPO_ROOT}/.venv/bin/python"
SCRIPT="${REPO_ROOT}/scripts/check-npm-unlocks.py"
LOG_FILE="${ALGOVAULT_NPM_UNLOCK_LOG:-/var/log/algovault-bot/check-npm-unlocks.log}"

mkdir -p "$(dirname "${LOG_FILE}")"

if [ -f /etc/algovault/bot.env ]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/algovault/bot.env
  set +a
fi

echo "[check-npm-unlocks] start ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${LOG_FILE}"
if "${PY}" "${SCRIPT}" "$@" 2>&1 | tee -a "${LOG_FILE}"; then
  echo "[check-npm-unlocks] done ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${LOG_FILE}"
  exit 0
fi
echo "[check-npm-unlocks] ERROR exit non-zero" | tee -a "${LOG_FILE}" >&2
exit 2
