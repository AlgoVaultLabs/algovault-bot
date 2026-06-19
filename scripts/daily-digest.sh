#!/usr/bin/env bash
# TG-BROADCAST-STACK-W1 CH2 (2026-05-28): daily-digest cron wrapper.
#
# Invoked by Hetzner crontab `3 12 * * * /opt/algovault-bot/scripts/daily-digest.sh`
# Reads env from /etc/algovault-bot/env (the SAME EnvironmentFile the bot +
# alert-engine systemd units use) for PUBLIC_BOT_TOKEN, ALGOVAULT_MCP_URL,
# ALGOVAULT_INTERNAL_BYPASS_KEY, BOT_ADMIN_CHAT_IDS, ADOPTION_BROADCASTS_LIVE.
# TG-WATCH-ADOPTION-BROADCAST-W1 (2026-06-19): FIXED the env path — it used to
# source /etc/algovault/bot.env which never existed, so every fire exited
# `status: no_token` (the digest never actually sent). Logs to
# /var/log/algovault-bot/daily-digest.log (logrotate-managed; weekly/8/gzip).
#
# Exit codes:
#   0  — success (broadcast sent OR suppressed_duplicate OR dry-run)
#   2  — daily-digest.py exited non-zero
#   *  — any other propagates via `set -e`

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/algovault-bot}"
PY="${REPO_ROOT}/.venv/bin/python"
DIGEST_SCRIPT="${REPO_ROOT}/scripts/daily-digest.py"
LOG_FILE="${ALGOVAULT_DIGEST_LOG:-/var/log/algovault-bot/daily-digest.log}"

mkdir -p "$(dirname "${LOG_FILE}")"

# Load env (the bot/alert-engine EnvironmentFile). Without this the cron has no
# PUBLIC_BOT_TOKEN and sendBroadcast refuses (status: no_token).
if [ -f /etc/algovault-bot/env ]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/algovault-bot/env
  set +a
fi

if [ ! -x "${PY}" ]; then
  echo "[daily-digest] ERROR: python venv not found at ${PY}" >&2
  exit 2
fi

if [ ! -f "${DIGEST_SCRIPT}" ]; then
  echo "[daily-digest] ERROR: daily-digest.py not found at ${DIGEST_SCRIPT}" >&2
  exit 2
fi

# Pass through any CLI args (e.g. --dry-run or --cohort-override=mr1-only
# for verification-gate use). Default invocation = real broadcast.
echo "[daily-digest] starting ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${LOG_FILE}"
if "${PY}" "${DIGEST_SCRIPT}" "$@" 2>&1 | tee -a "${LOG_FILE}"; then
  echo "[daily-digest] done ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${LOG_FILE}"
  exit 0
else
  echo "[daily-digest] ERROR: daily-digest.py exited non-zero" | tee -a "${LOG_FILE}" >&2
  exit 2
fi
