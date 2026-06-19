#!/usr/bin/env bash
# TG-BROADCAST-STACK-W1 CH6 (2026-05-28): cron wrapper for check-npm-unlocks.py.
# Cadence: every 10 min (per architect Q-C ratification — avoids 5m grid collision).
#
# Reads env from /etc/algovault-bot/env (the SAME EnvironmentFile the bot +
# alert-engine systemd units use) for PUBLIC_BOT_TOKEN (needed by sendDM to
# deliver the "verified — 30-day Pro" / "expired" DMs).
# OPS-BOT-NPM-UNLOCK-ENV-PATH-W1 (2026-06-19): FIXED the env path — it used to
# source /etc/algovault/bot.env which never existed, so the verification cron
# ran without PUBLIC_BOT_TOKEN and could grant Pro in the DB but never DM the
# subscriber (silent grant). Mirrors the daily-digest.sh fix in f0115fe.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/algovault-bot}"
PY="${REPO_ROOT}/.venv/bin/python"
SCRIPT="${REPO_ROOT}/scripts/check-npm-unlocks.py"
LOG_FILE="${ALGOVAULT_NPM_UNLOCK_LOG:-/var/log/algovault-bot/check-npm-unlocks.log}"

mkdir -p "$(dirname "${LOG_FILE}")"

if [ -f /etc/algovault-bot/env ]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/algovault-bot/env
  set +a
fi

echo "[check-npm-unlocks] start ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${LOG_FILE}"
if "${PY}" "${SCRIPT}" "$@" 2>&1 | tee -a "${LOG_FILE}"; then
  echo "[check-npm-unlocks] done ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${LOG_FILE}"
  exit 0
fi
echo "[check-npm-unlocks] ERROR exit non-zero" | tee -a "${LOG_FILE}" >&2
exit 2
