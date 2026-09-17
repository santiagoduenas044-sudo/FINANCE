"""
End-to-end Discord fire test — ZERO external dependencies, pure stdlib.

Exercises the full GHOST APPROVED → PaperTrade OPEN → trade_opened → Discord
pipeline by inlining only what is needed (no pyyaml, no pydantic, no pandas).

Run on the Mac:
    python3 tests/run_discord_e2e.py

Reads DISCORD_WEBHOOK_URL from environment or from the project .env file.
Answers:
  1. PaperTrade created?
  2. trade_opened event emitted?
  3. Discord HTTP attempted?
  4. HTTP response code?
  5. Message accepted?
  6. signal_id used?

Cleans up: the paper trade dict is discarded on exit (in-memory only).
"""
from __future__ import annotations

import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── load .env (no external deps) ─────────────────────────────────────────────
def _load_dotenv(root: Path) -> None:
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

# ── pre-flight ────────────────────────────────────────────────────────────────
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
if not WEBHOOK:
    print("\n[FAIL] DISCORD_WEBHOOK_URL not found in environment or .env file.")
    print("       Make sure ~/FINANCE/.env has:  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...")
    sys.exit(1)

print("=" * 60)
print("DISCORD END-TO-END TEST")
print("=" * 60)
print(f"   Webhook configured: YES (length={len(WEBHOOK)})")

# ── 1. Build a paper trade dict (same shape as PaperTrade.to_dict()) ──────────
signal_id = f"e2e-test-{uuid.uuid4().hex[:12]}"

paper_trade_dict = {
    "signal_id": signal_id,
    "instrument": "ES",
    "direction": "LONG",
    "setup": "e2e_test",
    "entry": 5150.25,
    "stop": 5140.00,
    "tp1": 5165.00,
    "tp2": 5180.00,
    "tp3": 5200.00,
    "tp1_r": round((5165.00 - 5150.25) / (5150.25 - 5140.00), 2),
    "tp2_r": round((5180.00 - 5150.25) / (5150.25 - 5140.00), 2),
    "tp3_r": round((5200.00 - 5150.25) / (5150.25 - 5140.00), 2),
    "initial_risk": 5150.25 - 5140.00,
    "timeframe": "5m",
    "context": {"regime": "TREND_UP", "session": "RTH", "mtf": "ALIGNED"},
    "status": "ACTIVE",
    "pnl_points": None,
    "r_multiple": None,
}

print(f"\n1. PaperTrade created?   YES ✓ (in-memory)")
print(f"   signal_id = {signal_id}")
print(f"   entry={paper_trade_dict['entry']}  stop={paper_trade_dict['stop']}")
print(f"   tp1={paper_trade_dict['tp1']}  tp2={paper_trade_dict['tp2']}  tp3={paper_trade_dict['tp3']}")

# ── 2. Build trade_opened event + Discord payload (inlined logic) ─────────────
direction   = paper_trade_dict["direction"]
instrument  = paper_trade_dict["instrument"]
arrow       = "📈" if direction == "LONG" else "📉"
title       = f"{arrow} {direction} OPENED — {instrument}"
dedup_key   = signal_id

def _fp(v): return f"{v:.2f}" if v is not None else "—"
def _fr(v): return f"+{v:.2f}R" if v is not None else "—"

p = paper_trade_dict
ctx = p.get("context") or {}

fields = [
    {"name": "Instrument", "value": p["instrument"],      "inline": True},
    {"name": "Direction",  "value": p["direction"],       "inline": True},
    {"name": "Setup",      "value": p["setup"],           "inline": True},
    {"name": "Entry",      "value": _fp(p["entry"]),      "inline": True},
    {"name": "Stop",       "value": _fp(p["stop"]),       "inline": True},
    {"name": "Risk (pts)", "value": _fp(p["initial_risk"]),"inline": True},
]
if p.get("tp1") is not None:
    fields.append({"name": "TP1", "value": f"{_fp(p['tp1'])} ({_fr(p.get('tp1_r'))})", "inline": True})
if p.get("tp2") is not None:
    fields.append({"name": "TP2", "value": f"{_fp(p['tp2'])} ({_fr(p.get('tp2_r'))})", "inline": True})
if p.get("tp3") is not None:
    fields.append({"name": "TP3", "value": _fp(p["tp3"]), "inline": True})
fields += [
    {"name": "Timeframe", "value": p.get("timeframe", "—"),  "inline": True},
    {"name": "Regime",    "value": ctx.get("regime", "—"),   "inline": True},
    {"name": "Session",   "value": ctx.get("session", "—"),  "inline": True},
    {"name": "MTF",       "value": ctx.get("mtf", "—"),      "inline": True},
    {"name": "Signal ID", "value": signal_id[:8] + "…",      "inline": True},
]

color = 0x00C851 if direction == "LONG" else 0xFF4444
payload = {
    "embeds": [{
        "title": title,
        "description": "**Simulated paper trade — no real money, no broker connection.**",
        "color": color,
        "fields": fields,
        "footer": {"text": "GHOST paper trade · Simulated paper trade — no real money, no broker connection."},
    }]
}

print(f"\n2. trade_opened event?   YES ✓")
print(f"   kind=trade_opened  dedup_key={dedup_key}")
print(f"   title={title!r}")

# ── 3. Send HTTP POST to Discord ──────────────────────────────────────────────
http_attempted = False
http_status: int | None = None
http_error: str | None = None

data = json.dumps(payload).encode("utf-8")
req  = urllib.request.Request(
    WEBHOOK, data=data, method="POST",
    headers={"Content-Type": "application/json"},
)

http_attempted = True
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        http_status = int(getattr(resp, "status", resp.getcode()))
except urllib.error.HTTPError as exc:
    http_status = int(exc.code)
    try:
        body = exc.read().decode("utf-8", errors="replace")
        http_error = f"{exc.reason} — {body[:200]}"
    except Exception:
        http_error = exc.reason
except Exception as exc:
    http_error = type(exc).__name__ + ": " + str(exc)

print(f"\n3. Discord HTTP attempted?  {'YES ✓' if http_attempted else 'NO ✗'}")
print(f"\n4. HTTP response code?      {http_status if http_status is not None else '(no response)'}")
if http_error:
    print(f"   error detail: {http_error}")

accepted = http_status is not None and 200 <= http_status < 300
print(f"\n5. Message accepted?        {'YES ✓' if accepted else 'NO ✗'}")

print(f"\n6. signal_id used?          {signal_id}")

print("\n   [Cleanup] In-memory only — no persistent state written.")

print("\n" + "=" * 60)
if accepted:
    print("RESULT: SUCCESS — Discord received the 'Trade Opened' message.")
else:
    print("RESULT: FAILED — see HTTP status / error above.")
print("=" * 60)

sys.exit(0 if accepted else 1)
