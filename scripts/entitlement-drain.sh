#!/usr/bin/env bash
# PRICING-BOT-DELIVERY-METERING-W1 / CH4f — plan-debit outbox drain cron wrapper.
#
# Hetzner crontab — read it, do NOT trust this comment for the cadence:
#   crontab -l | grep entitlement-drain
#
# 🛑 THE SCHEDULE IS NOT `*/5`, AND THIS LINE SAID IT WAS. Measured 2026-09-07, the live entry is
#   13,17,23,27,33,37,43,47,53,57 * * * * /opt/algovault-bot/scripts/entitlement-drain.sh >> ...
# — TEN fires an hour on deliberately off-`:00` offsets (they dodge the seeding crons), with
# alternating 4- and 6-minute gaps and a SIXTEEN-minute gap across the hour boundary, :57 -> :13.
# The sibling `referral-notify-drain.sh` genuinely is `*/5`, which is the easy confusion. Any
# claim of the form "the drain will pick this up within 5 minutes" is false; the worst case is 16.
# Corrected by GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH2, whose wall-follow-up pass
# rides this same schedule — deliberately, because a cadence with its own timer is one nobody can
# reason about alongside the one already running.
#
# Reads /etc/algovault-bot/env (the SAME EnvironmentFile the bot units use) for
# ALGOVAULT_MCP_URL and
# ALGOVAULT_INTERNAL_BYPASS_KEY (the entitlement API auth). Logs to
# /var/log/algovault-bot/entitlement-drain.log. NEVER uses send_telegram.sh.
#
# Exit: 0 success · 2 venv/script missing · * propagates via set -e.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/algovault-bot}"
PY="${REPO_ROOT}/.venv/bin/python"
SCRIPT="${REPO_ROOT}/scripts/entitlement-drain.py"
LOG_FILE="${ALGOVAULT_DRAIN_LOG:-/var/log/algovault-bot/entitlement-drain.log}"
mkdir -p "$(dirname "${LOG_FILE}")"

if [ -f /etc/algovault-bot/env ]; then
  set -a
  # shellcheck disable=SC1091
  . /etc/algovault-bot/env
  set +a
fi

[ -x "${PY}" ] || { echo "[entitlement-drain] ERROR: python venv not found at ${PY}" >&2; exit 2; }
[ -f "${SCRIPT}" ] || { echo "[entitlement-drain] ERROR: script not found at ${SCRIPT}" >&2; exit 2; }

exec "${PY}" "${SCRIPT}" "$@"
