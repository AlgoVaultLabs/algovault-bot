#!/usr/bin/env bash
# tg-conversion-baseline.sh — GROWTH-TG-PLAN-PICKER-W1 Step 0.
#
# The BASELINE INSTRUMENT this wave and its successor (GROWTH-TG-STARS-CHECKOUT-W1) are
# judged against. It is a script rather than two pasted queries for exactly one reason: a
# before/after delta measured with two different instruments is not a delta
# (`monitoring-and-recovery.md` — "a measured baseline is meaningless without its
# instrument"). The successor re-runs THIS FILE, unchanged.
#
# STRICTLY READ-ONLY. Two hosts, two stores:
#   S0.1  bot SQLite  /var/lib/algovault-bot/state.db   opened `file:...?mode=ro`
#   S0.2  server PG   crypto-quant-signal-mcp-postgres-1, role `aoe_readonly` (SELECT only)
#
# 🛑 CONTRACT: prints exactly ONE terminal line, `TG_BASELINE_VERDICT=PASS|INDETERMINATE`.
# Callers read the TOKEN, never the exit code (`verification-gates.md`). Exit 0 = PASS,
# exit 3 = INDETERMINATE (the token-law default for a NEW gate). It BLOCKS NOTHING — an
# unreachable host is INDETERMINATE and no wave stops for it.
#
# Vacuity discipline: a count of ZERO is a FACT about the world and is reported as a PASS
# with an explicit positive line. A count we were HANDED and could not PARSE is
# INDETERMINATE. Empty-vs-unparseable is the line, not empty-vs-non-empty.
#
# Runs from the operator's machine (ssh) or on signal-1 itself (auto-detected).
#
# Usage:
#   bash scripts/tg-conversion-baseline.sh
#   bash scripts/tg-conversion-baseline.sh --self-test
set -uo pipefail

HOST=${TG_BASELINE_HOST:-root@204.168.185.24}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/algovault_deploy}
DB_PATH=${TG_BASELINE_DB:-/var/lib/algovault-bot/state.db}
PG_CTR=${TG_BASELINE_PG_CTR:-crypto-quant-signal-mcp-postgres-1}
PG_ROLE=${TG_BASELINE_PG_ROLE:-aoe_readonly}
WINDOW_DAYS=${TG_BASELINE_WINDOW_DAYS:-90}

# ── pure helpers (the only things `--self-test` can honestly cover) ───────────
#
# A hermetic self-test is structurally blind to exactly what its own seam replaces
# (`verification-gates.md`), and this script's seam IS the ssh + the two query engines.
# So the self-test covers the verdict decision and the numeric guard, and NOTHING claims
# the SQL is exercised. The first live run is the test of the SQL.

# is_count <string> — true iff the argument is a bare non-negative integer.
# A blank, an error string, or a psql NOTICE all fail here, which is what turns
# "we were handed something we could not parse" into INDETERMINATE rather than 0.
is_count() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

# verdict <s01_ok> <s02_ok> — PASS only when BOTH stores answered.
verdict() {
  if [ "${1:-0}" = "1" ] && [ "${2:-0}" = "1" ]; then echo PASS; else echo INDETERMINATE; fi
}

self_test() {
  local pass=0 fail=0
  chk() { # chk <label> <expected> <actual>
    if [ "$2" = "$3" ]; then pass=$((pass + 1)); else
      fail=$((fail + 1)); echo "  SELF-TEST FAIL: $1 — expected '$2', got '$3'"
    fi
  }
  # is_count — must accept a real count INCLUDING zero, and reject every non-count.
  chk "is_count 0"        "yes" "$(is_count 0        && echo yes || echo no)"
  chk "is_count 26"       "yes" "$(is_count 26       && echo yes || echo no)"
  chk "is_count ''"       "no"  "$(is_count ''       && echo yes || echo no)"
  chk "is_count 'ERROR'"  "no"  "$(is_count 'ERROR'  && echo yes || echo no)"
  chk "is_count '-1'"     "no"  "$(is_count '-1'     && echo yes || echo no)"
  chk "is_count '1 row'"  "no"  "$(is_count '1 row'  && echo yes || echo no)"
  # verdict — two-way: it must be able to say PASS *and* to refuse.
  chk "verdict 1 1" "PASS"          "$(verdict 1 1)"
  chk "verdict 1 0" "INDETERMINATE" "$(verdict 1 0)"
  chk "verdict 0 1" "INDETERMINATE" "$(verdict 0 1)"
  chk "verdict 0 0" "INDETERMINATE" "$(verdict 0 0)"
  # Vacuity guard on the suite itself: a run that asserted nothing must never read green.
  if [ "$pass" -eq 0 ]; then
    echo "SELF-TEST: no assertions ran — the fixture built nothing"
    echo "TG_BASELINE_VERDICT=INDETERMINATE"; exit 3
  fi
  echo "SELF-TEST: $pass passed, $fail failed"
  if [ "$fail" -ne 0 ]; then echo "TG_BASELINE_VERDICT=INDETERMINATE"; exit 3; fi
  echo "TG_BASELINE_VERDICT=PASS"; exit 0
}

[ "${1:-}" = "--self-test" ] && self_test

# ── transport: ssh from the Mac, or straight through when already on signal-1 ─
if [ -r "$DB_PATH" ] || [ -S /var/run/docker.sock ]; then
  ON_HOST=1
else
  ON_HOST=0
fi
run_remote() { # run_remote <sh -c body>
  if [ "$ON_HOST" = "1" ]; then bash -c "$1"; else
    ssh -i "$SSH_KEY" -o ConnectTimeout=15 -o BatchMode=yes "$HOST" "$1"
  fi
}

echo "── TG conversion baseline (pre-picker) ─────────────────────────────────"
echo "generated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)   window: ${WINDOW_DAYS}d"
echo "instrument:   scripts/tg-conversion-baseline.sh @ $(git rev-parse --short=8 HEAD 2>/dev/null || echo unknown)"
echo

# ── S0.1 — the bot's own SQLite: does the WALL drive the blocks? ─────────────
#
# `blocked_after_wall` carries BOTH walls. The monthly one stamps an ISO instant
# (quota_100_last_fired_at); the daily one stamps a UTC DAY KEY (quota_day_notice_day),
# so it is compared with date(), never julianday() — a day key is not an instant and
# treating it as one silently reads every daily wall as midnight.
#
# The `walled_now` cohort is keyed on the NOTICE STAMP, not on a re-derived
# `alert_count >= quota` predicate. `quota.count_walled_now` projects from
# `evaluate_delivery`, and a second SQL derivation of that decision is exactly the
# drift `quota.py:981-985` already refuses. What this reports is therefore an
# OBSERVATION of the notice log, and it is labelled as one.
S01_SQL=$(cat <<'SQL'
.mode list
.separator |
SELECT 'blocked_total',            count(*) FROM subscribers WHERE bot_blocked_at IS NOT NULL;
SELECT 'blocked_after_wall_month', count(*) FROM subscribers
  WHERE bot_blocked_at IS NOT NULL AND quota_100_last_fired_at IS NOT NULL
    AND julianday(bot_blocked_at) > julianday(quota_100_last_fired_at);
SELECT 'blocked_after_wall_daily', count(*) FROM subscribers
  WHERE bot_blocked_at IS NOT NULL AND quota_day_notice_day IS NOT NULL
    AND date(bot_blocked_at) >= quota_day_notice_day;
SELECT 'blocked_never_walled',     count(*) FROM subscribers
  WHERE bot_blocked_at IS NOT NULL AND quota_100_last_fired_at IS NULL
    AND quota_day_notice_day IS NULL;
SELECT 'blocked_with_alert_24h_before', count(*) FROM subscribers s
  WHERE s.bot_blocked_at IS NOT NULL AND EXISTS (
    SELECT 1 FROM alerts_fired a WHERE a.chat_id = s.chat_id
      AND julianday(a.fired_at) > julianday(s.bot_blocked_at) - 1.0
      AND julianday(a.fired_at) <= julianday(s.bot_blocked_at));
SELECT 'blocked_within_24h_of_start', count(*) FROM subscribers
  WHERE bot_blocked_at IS NOT NULL AND created_at IS NOT NULL
    AND julianday(bot_blocked_at) - julianday(created_at) < 1.0;
SELECT 'walled_now_notified',      count(*) FROM subscribers
  WHERE bot_blocked_at IS NULL AND quota_100_last_fired_at IS NOT NULL
    AND alerts_window_start IS NOT NULL
    AND julianday('now') - julianday(alerts_window_start) < 30.0;
WITH d AS (
  SELECT julianday(bot_blocked_at)
         - julianday(COALESCE(quota_100_last_fired_at, quota_day_notice_day)) AS days
  FROM subscribers
  WHERE bot_blocked_at IS NOT NULL AND (
      (quota_100_last_fired_at IS NOT NULL
        AND julianday(bot_blocked_at) > julianday(quota_100_last_fired_at))
   OR (quota_day_notice_day IS NOT NULL AND date(bot_blocked_at) >= quota_day_notice_day))
)
SELECT 'median_days_wall_to_block',
  CASE WHEN (SELECT count(*) FROM d) = 0 THEN 'n/a (empty cohort)'
  ELSE (SELECT round(avg(days), 2) FROM (
         SELECT days FROM d ORDER BY days
         LIMIT 2 - (SELECT count(*) FROM d) % 2
         OFFSET (SELECT (count(*) - 1) / 2 FROM d))) END;
SQL
)

S01_COHORT_SQL=$(cat <<'SQL'
.mode list
.separator |
SELECT chat_id,
       round(julianday(alerts_window_start) + 30.0 - julianday('now'), 2)
FROM subscribers
WHERE bot_blocked_at IS NULL AND quota_100_last_fired_at IS NOT NULL
  AND alerts_window_start IS NOT NULL
  AND julianday('now') - julianday(alerts_window_start) < 30.0
ORDER BY 2;
SQL
)

S01_OK=0
echo "S0.1 — bot SQLite (read-only): does the wall drive the blocks?"
s01_out=$(run_remote "sqlite3 -readonly 'file:${DB_PATH}?mode=ro' <<'EOSQL'
${S01_SQL}
EOSQL" 2>&1)
s01_rc=$?
blocked_total=$(printf '%s\n' "$s01_out" | awk -F'|' '$1=="blocked_total"{print $2}')
if [ "$s01_rc" -ne 0 ] || ! is_count "$blocked_total"; then
  echo "  UNREADABLE — the store answered with something this script cannot parse:"
  printf '%s\n' "$s01_out" | sed 's/^/    /' | head -12
else
  printf '%s\n' "$s01_out" | awk -F'|' '{printf "  %-32s %s\n", $1, $2}'
  if [ "$blocked_total" -eq 0 ]; then
    echo "  (blocked_total is ZERO — a fact about the world, not a failed read)"
  fi
  echo "  walled_now cohort — days of silence remaining (alerts_window_start + 30d - now):"
  cohort=$(run_remote "sqlite3 -readonly 'file:${DB_PATH}?mode=ro' <<'EOSQL'
${S01_COHORT_SQL}
EOSQL" 2>&1)
  if [ -z "$cohort" ]; then
    echo "    (cohort is empty — nobody is currently walled and notified)"
  else
    printf '%s\n' "$cohort" | awk -F'|' '{printf "    chat %-14s %s d\n", $1, $2}'
  fi
  S01_OK=1
fi
echo

# ── S0.2 — server PG: TG clicks → TG conversions, per campaign ───────────────
#
# The join column is `client_reference_id`, DECLARED by both schemas:
# `signup_attribution` (subscriber-attribution.ts CREATE_SIGNUP_ATTRIBUTION_SQL, PRIMARY KEY)
# and `subscriber_profiles` (CREATE_SUBSCRIBER_PROFILES_SQL). Read, never guessed.
#
# LEFT JOIN, not INNER: an unconverted click must stay in the denominator. An inner join
# would silently report a 100% conversion rate over whoever happened to convert.
S02_SQL="SELECT a.utm_campaign,
                count(*) AS clicks,
                count(p.client_reference_id) FILTER (WHERE p.converted_at IS NOT NULL) AS conversions
         FROM signup_attribution a
         LEFT JOIN subscriber_profiles p ON p.client_reference_id = a.client_reference_id
         WHERE a.utm_source = 'tg_bot'
           AND a.created_at > now() - interval '${WINDOW_DAYS} days'
         GROUP BY 1 ORDER BY 2 DESC, 1;"
S02_TOTAL_SQL="SELECT 'TOTAL', count(*),
                count(p.client_reference_id) FILTER (WHERE p.converted_at IS NOT NULL)
         FROM signup_attribution a
         LEFT JOIN subscriber_profiles p ON p.client_reference_id = a.client_reference_id
         WHERE a.utm_source = 'tg_bot'
           AND a.created_at > now() - interval '${WINDOW_DAYS} days';"
# Cross-check from the OTHER side: profiles whose own channel slug resolved to tg_bot.
# Two projections of one funnel; a divergence is a finding, not noise.
S02_XCHK_SQL="SELECT count(*) FILTER (WHERE converted_at IS NOT NULL), count(*)
         FROM subscriber_profiles WHERE channel = 'tg_bot';"

S02_OK=0
echo "S0.2 — server PG as ${PG_ROLE} (SELECT-only): tg_bot clicks -> conversions"
s02_out=$(run_remote "docker exec ${PG_CTR} psql -U ${PG_ROLE} -d \"\$(docker exec ${PG_CTR} printenv POSTGRES_DB)\" -tAq -F'|' -c \"${S02_SQL}\" && docker exec ${PG_CTR} psql -U ${PG_ROLE} -d \"\$(docker exec ${PG_CTR} printenv POSTGRES_DB)\" -tAq -F'|' -c \"${S02_TOTAL_SQL}\"" 2>&1)
s02_rc=$?
total_clicks=$(printf '%s\n' "$s02_out" | awk -F'|' '$1=="TOTAL"{print $2}')
if [ "$s02_rc" -ne 0 ] || ! is_count "$total_clicks"; then
  echo "  UNREADABLE — the store answered with something this script cannot parse:"
  printf '%s\n' "$s02_out" | sed 's/^/    /' | head -12
else
  printf "  %-28s %8s %12s\n" "campaign" "clicks" "conversions"
  printf '%s\n' "$s02_out" | awk -F'|' 'NF>=3{printf "  %-28s %8s %12s\n", $1, $2, $3}'
  if [ "$total_clicks" -eq 0 ]; then
    echo "  (zero tg_bot clicks in the window — a fact about the world, not a failed read)"
  fi
  xchk=$(run_remote "docker exec ${PG_CTR} psql -U ${PG_ROLE} -d \"\$(docker exec ${PG_CTR} printenv POSTGRES_DB)\" -tAq -F'|' -c \"${S02_XCHK_SQL}\"" 2>&1)
  echo "  cross-check subscriber_profiles.channel='tg_bot' (converted|total): ${xchk}"
  S02_OK=1
fi
echo

V=$(verdict "$S01_OK" "$S02_OK")
echo "TG_BASELINE_VERDICT=${V}"
[ "$V" = "PASS" ] && exit 0
exit 3
