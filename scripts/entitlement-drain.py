#!/usr/bin/env python3
"""Host-cron entry for the plan-debit drain (PRICING-BOT-DELIVERY-METERING-W1 / CH4f).

   --dry-run reports what would be drained without POSTing, stamping or polling.

   Since GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH2 it also runs the wall-follow-up
   pass, and --dry-run additionally prints that pass's CANDIDATE TABLE plus a terminal
   `WALL_FOLLOWUP_DRYRUN_VERDICT=PASS|INDETERMINATE`. That table is the artifact a human reads
   before `ALGOVAULT_WALL_FOLLOWUPS_ENABLED=1` is set: the cadence DMs real people, so the
   go-live step is "read who it would reach", not "read a count".

   The flag is NOT needed for the dry run — the table renders while the sender is dark, which is
   the only order in which a human can approve it before it can send anything.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the bot package importable when run as a CLI from cron.
_PKG_PARENT = Path(__file__).resolve().parent.parent / "src"
if _PKG_PARENT.is_dir() and str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from algovault_bot.entitlement_drain import drain_entitlement_debits  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AlgoVault plan-debit outbox drain")
    p.add_argument("--dry-run", action="store_true", help="report only; no POST, no stamp, no poll")
    args = p.parse_args(argv)
    res = drain_entitlement_debits(dry_run=args.dry_run)
    print(f"[entitlement-drain] {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
