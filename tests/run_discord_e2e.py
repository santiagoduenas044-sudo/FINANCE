"""
End-to-end Discord test: GHOST APPROVED → PaperTrade OPEN → trade_opened → Discord.

Run on the Mac (where DISCORD_WEBHOOK_URL is set):
    cd ~/FINANCE
    python tests/run_discord_e2e.py

Answers clearly:
  1. PaperTrade created?
  2. trade_opened event emitted?
  3. Discord HTTP attempted?
  4. HTTP response code?
  5. Message accepted?
  6. signal_id used?

Cleans up: closes the paper trade at the end so it doesn't pollute live state.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

# Make sure the local package is importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signal_engine.config.schema import DiscordOutputConfig
from signal_engine.output.discord import DiscordOutputAdapter
from signal_engine.output.events import OutputEvent
from signal_engine.paper.models import ExitReason, TradeStatus
from signal_engine.paper.tracker import PaperTradeTracker
import pandas as pd

# ── 0. Pre-flight ─────────────────────────────────────────────────────────────
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
if not WEBHOOK:
    print("\n[FAIL] DISCORD_WEBHOOK_URL is not set in this environment.")
    print("       Run this script on the Mac where the bot is configured.")
    sys.exit(1)

print("=" * 60)
print("DISCORD END-TO-END TEST")
print("=" * 60)

# ── 1. Open a paper trade using the real tracker ──────────────────────────────
tracker = PaperTradeTracker()
signal_id = f"e2e-test-{uuid.uuid4().hex[:12]}"
ts = pd.Timestamp.now(tz="UTC")

paper_trade = tracker.open_trade(
    signal_id=signal_id,
    instrument="ES",
    direction="LONG",
    setup="e2e_test",
    entry=5150.25,
    stop=5140.00,
    take_profits=[5165.00, 5180.00, 5200.00],
    opened_at=ts,
)

created = paper_trade is not None
print(f"\n1. PaperTrade created?   {'YES ✓' if created else 'NO ✗'}")
if not created:
    print("   tracker.open_trade() returned None — signal already processed or open trade exists.")
    sys.exit(1)

print(f"   signal_id = {signal_id}")
print(f"   entry={paper_trade.entry}  stop={paper_trade.stop}")
print(f"   tp1={paper_trade.take_profits[0] if paper_trade.take_profits else None}"
      f"  tp2={paper_trade.take_profits[1] if len(paper_trade.take_profits) > 1 else None}"
      f"  tp3={paper_trade.take_profits[2] if len(paper_trade.take_profits) > 2 else None}")

# ── 2. Build OutputEvent.from_trade_opened ────────────────────────────────────
trade_dict = paper_trade.to_dict()
event = OutputEvent.from_trade_opened(trade_dict)

emitted = event is not None and event.kind == "trade_opened" and event.dedup_key == signal_id
print(f"\n2. trade_opened event?   {'YES ✓' if emitted else 'NO ✗'}")
print(f"   kind={event.kind}  dedup_key={event.dedup_key}")
print(f"   title={event.title!r}")

# ── 3. Build adapter with real webhook; capture HTTP response ─────────────────
http_attempted = False
http_status = None
http_error = None

def _capturing_transport(url: str, data: bytes, timeout: float) -> int:
    global http_attempted, http_status, http_error
    http_attempted = True
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            http_status = int(getattr(resp, "status", resp.getcode()))
            return http_status
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        http_error = exc.reason
        return http_status
    except Exception as exc:
        http_error = str(exc)
        return 0

cfg = DiscordOutputConfig(enabled=True, webhook_env="DISCORD_WEBHOOK_URL",
                          deduplication_enabled=True)
adapter = DiscordOutputAdapter(cfg, webhook_url=WEBHOOK,
                               transport=_capturing_transport)

# emit() returns True on success
result = adapter.emit(event)

print(f"\n3. Discord HTTP attempted?  {'YES ✓' if http_attempted else 'NO ✗'}")
print(f"\n4. HTTP response code?      {http_status if http_status else '(no response)'}")
if http_error:
    print(f"   error detail: {http_error}")

accepted = result is True and http_status is not None and 200 <= http_status < 300
print(f"\n5. Message accepted?        {'YES ✓' if accepted else 'NO ✗'}")
if not accepted and http_status:
    print(f"   HTTP {http_status} — message was NOT delivered.")

print(f"\n6. signal_id used?          {signal_id}")

# ── 4. Clean up: close the paper trade so it doesn't linger ───────────────────
# Use the tracker's internal _close() — same path as a real stop-out.
tracker._close(paper_trade, when=pd.Timestamp.now(tz="UTC"),
               price=paper_trade.entry, reason=ExitReason.CANCELLED,
               status=TradeStatus.CANCELLED)
tracker._reconcile()   # moves it out of open_trades

print("\n   [Cleanup] Paper trade closed and moved to history (not in open_trades).")

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if accepted:
    print("RESULT: SUCCESS — Discord received the 'Trade Opened' message.")
else:
    print("RESULT: FAILED — see HTTP status / error above.")
print("=" * 60)

sys.exit(0 if accepted else 1)
