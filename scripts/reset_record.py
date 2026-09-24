"""Start a clean trade record for a new strategy — without deleting anything.

Moves the paper-trade history, the signal/GHOST logs and the trade-idea
numbering into data_store/archive/<timestamp>/, so the dashboard, !status and
the daily numbering start from zero. Undo = move the files back.

Stop the bot first: a running bot keeps its trades in memory and would write
them straight back.

    python scripts/reset_record.py          # show what would move
    python scripts/reset_record.py --yes    # do it
"""
from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data_store"
TARGETS = ["paper", "signal_log.jsonl", "ghost_log.jsonl", "ideas.json"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yes", action="store_true", help="actually move the files")
    args = ap.parse_args()

    found = [STORE / t for t in TARGETS if (STORE / t).exists()]
    if not found:
        print("Nothing to archive — the record is already empty.")
        return 0
    dest = STORE / "archive" / datetime.now().strftime("%Y%m%d-%H%M%S")
    for p in found:
        print(f"  {p.relative_to(ROOT)}  ->  {(dest / p.name).relative_to(ROOT)}")
    if not args.yes:
        print("\nDry run. Stop the bot, then re-run with --yes.")
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    for p in found:
        shutil.move(str(p), str(dest / p.name))
    print(f"\nArchived to {dest.relative_to(ROOT)}. Start the bot: the record is at zero.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
