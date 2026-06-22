#!/usr/bin/env python3
"""Host-cron entry for the referral notification drain (REFERRAL-PARITY-NOTIFS-W1 / C2).

   --dry-run prints what would be sent without sending or marking delivered.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the bot package importable when run as a CLI from cron.
_PKG_PARENT = Path(__file__).resolve().parent.parent / "src"
if _PKG_PARENT.is_dir() and str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from algovault_bot.referral_drain import drain_referral_notifications  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AlgoVault referral notification drain")
    p.add_argument("--dry-run", action="store_true", help="print what would send; do not send or mark delivered")
    args = p.parse_args(argv)
    res = drain_referral_notifications(dry_run=args.dry_run)
    print(f"[referral-notify-drain] {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
