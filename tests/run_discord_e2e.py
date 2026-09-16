"""
End-to-end Discord test: GHOST APPROVED → PaperTrade OPEN → trade_opened → Discord.

Run on the Mac:
    cd ~/FINANCE
    python3 tests/run_discord_e2e.py

Reads DISCORD_WEBHOOK_URL from environment or from the project .env file.
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

import os
import sys
import uuid
import urllib.error
import urllib.request
from pathlib import Path

# ── package root ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── load .env if DISCORD_WEBHOOK_URL not already in environment ───────────────
def _load_dotenv(root: Path) -> None:
    """Minimal .env loader — no external deps, never leaks values."""
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

_load_dotenv(ROOT)

from signal_engine.config.schema import DiscordOutputConfig
from signal_engine.output.discord import DiscordOutputAdapter
from signal_engine.output.events import OutputEvent
from signal_engine.paper.models import ExitReason, TradeStatus
from signal_engine.paper.tracker import PaperTradeTracker
import pandas as pd

# ── 0. Pre-flight ─────────────────────────────────────────────────────────────
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
if not WEBHOOK:
    print("\n[FAIL] DISCORD_WEBHOOK_URL is not set.")
    print("       Make sure your .env file at ~/FINANCE/.env has DISCORD_WEBHOOK_URL=<url>")
    sys.exit(1)

print("=" * 60)
print("DISCORD END-TO-END TEST")
print("=" * 60)
print(f"   Webhook configured: YES (length={len(WEBHOOK)})")

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
    print("   tracker.open_trade() returned None — duplicate signal_id or open trade exists.")
    sys.exit(1)

tps = paper_trade.take_profits
print(f"   signal_id = {signal_id}")
print(f"   entry={paper_trade.entry}  stop={paper_trade.stop}")
print(f"   tp1={tps[0] if len(tps) > 0 else None}"
      f"  tp2={tps[1] if len(tps) > 1 else None}"
      f"  tp3={tps[2] if len(tps) > 2 else None}")

# ── 2. Build OutputEvent.from_trade_opened ────────────────────────────────────
trade_dict = paper_trade.to_dict()
event = OutputEvent.from_trade_opened(trade_dict)

emitted = (event is not None
           and event.kind == "trade_opened"
           and event.dedup_key == signal_id)
print(f"\n2. trade_opened event?   {'YES ✓' if emitted else 'NO ✗'}")
print(f"   kind={event.kind}  dedup_key={event.dedup_key}")
print(f"   title={event.title!r}")

# ── 3. Send to Discord with real webhook; capture response ────────────────────
http_attempted = False
http_status: int | None = None
http_error: str | None = None


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
        http_error = type(exc).__name__ + ": " + str(exc)
        return 0


cfg = DiscordOutputConfig(
    enabled=True,
    webhook_env="DISCORD_WEBHOOK_URL",
    deduplication_enabled=True,
)
adapter = DiscordOutputAdapter(cfg, webhook_url=WEBHOOK,
                               transport=_capturing_transport)
result = adapter.emit(event)

print(f"\n3. Discord HTTP attempted?  {'YES ✓' if http_attempted else 'NO ✗'}")
print(f"\n4. HTTP response code?      {http_status if http_status is not None else '(no response)'}")
if http_error:
    print(f"   error detail: {http_error}")

accepted = (result is True
            and http_status is not None
            and 200 <= http_status < 300)
print(f"\n5. Message accepted?        {'YES ✓' if accepted else 'NO ✗'}")
if not accepted and http_status:
    print(f"   HTTP {http_status} — message was NOT delivered.")

print(f"\n6. signal_id used?          {signal_id}")

# ── 4. Clean up ───────────────────────────────────────────────────────────────
tracker._close(paper_trade, when=pd.Timestamp.now(tz="UTC"),
               price=paper_trade.entry, reason=ExitReason.CANCELLED,
               status=TradeStatus.CANCELLED)
tracker._reconcile()

print("\n   [Cleanup] Paper trade closed and removed from open_trades.")

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if accepted:
    print("RESULT: SUCCESS — Discord received the 'Trade Opened' message.")
else:
    print("RESULT: FAILED — see HTTP status / error above.")
print("=" * 60)

sys.exit(0 if accepted else 1)
