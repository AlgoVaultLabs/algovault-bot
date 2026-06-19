#!/usr/bin/env bash
# TG-WATCH-ADOPTION-BROADCAST-W1 (R3): weekly scan-showcase cron wrapper.
#
# Invoked by Hetzner crontab `17 13 * * 1 /opt/algovault-bot/scripts/scan-showcase.sh`
# (Monday 13:17 UTC — off-:00, collision-free). Sources /etc/algovault-bot/env
# (PUBLIC_BOT_TOKEN, ALGOVAULT_MCP_URL, ALGOVAULT_INTERNAL_BYPASS_KEY,
# BOT_ADMIN_CHAT_IDS, ADOPTION_BROADCASTS_LIVE). Real broadcast fires only when
# ADOPTION_BROADCASTS_LIVE=1; otherwise it logs skipped_not_live and exits 0.
# Logs to /var/log/algovault-bot/scan-showcase.log (logrotate-managed).
#
# Exit codes: 0 — success/suppressed/skipped/dry-run; 2 — script error.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/algovault-bot}"
PY="${REPO_ROOT}/.venv/bin/python"
SCRIPT="${REPO_ROOT}/scripts/scan-showcase.py"
LOG_FILE="${ALGOVAULT_SHOWCASE_LOG:-/var/log/algovault-bot/scan-showcase.log}"

mkdir -p "$(dirname "${LOG_FILE}")"

if [ -f /etc/algovault-bot/env ]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/algovault-bot/env
  set +a
fi

if [ ! -x "${PY}" ]; then
  echo "[scan-showcase] ERROR: python venv not found at ${PY}" >&2
  exit 2
fi
if [ ! -f "${SCRIPT}" ]; then
  echo "[scan-showcase] ERROR: scan-showcase.py not found at ${SCRIPT}" >&2
  exit 2
fi

echo "[scan-showcase] starting ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${LOG_FILE}"
if "${PY}" "${SCRIPT}" "$@" 2>&1 | tee -a "${LOG_FILE}"; then
  echo "[scan-showcase] done ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${LOG_FILE}"
  exit 0
else
  echo "[scan-showcase] ERROR: scan-showcase.py exited non-zero" | tee -a "${LOG_FILE}" >&2
  exit 2
fi
