#!/usr/bin/env bash
# REFERRAL-PARITY-NOTIFS-W1 / C2 — referral notification drain cron wrapper.
#
# Hetzner crontab (every 5 min):
#   */5 * * * * /opt/algovault-bot/scripts/referral-notify-drain.sh >> /var/log/algovault-bot/referral-notify-drain.log 2>&1
#
# Reads /etc/algovault-bot/env (the SAME EnvironmentFile the bot units use) for
# PUBLIC_BOT_TOKEN (the user-messaging Bot API token), ALGOVAULT_MCP_URL, and
# ALGOVAULT_INTERNAL_BYPASS_KEY (the engine notification-queue auth). Logs to
# /var/log/algovault-bot/referral-notify-drain.log. NEVER uses send_telegram.sh.
#
# Exit: 0 success · 2 venv/script missing · * propagates via set -e.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/algovault-bot}"
PY="${REPO_ROOT}/.venv/bin/python"
SCRIPT="${REPO_ROOT}/scripts/referral-notify-drain.py"
LOG_FILE="${ALGOVAULT_DRAIN_LOG:-/var/log/algovault-bot/referral-notify-drain.log}"
mkdir -p "$(dirname "${LOG_FILE}")"

if [ -f /etc/algovault-bot/env ]; then
  set -a
  # shellcheck disable=SC1091
  . /etc/algovault-bot/env
  set +a
fi

[ -x "${PY}" ] || { echo "[referral-notify-drain] ERROR: python venv not found at ${PY}" >&2; exit 2; }
[ -f "${SCRIPT}" ] || { echo "[referral-notify-drain] ERROR: script not found at ${SCRIPT}" >&2; exit 2; }

exec "${PY}" "${SCRIPT}" "$@"
