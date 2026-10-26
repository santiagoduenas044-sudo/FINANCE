"""Auditoría: every closed paper trade of every set up, in one file to share.

    bash deploy/macos/ctl.sh auditoria

Prints a summary per set up (trades, % won, net $, profit factor, worst run,
by session and by side) and writes auditoria-AAAA-MM-DD.json next to the repo:
trades and set up names only. No webhooks, tokens, passwords or users are in
it, so it can be sent to whoever reviews the bots.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from signal_engine.api.bots import trade_log  # noqa: E402
from signal_engine.config.schema import EngineConfig  # noqa: E402
from signal_engine.paper.tracker import PaperTradeTracker  # noqa: E402
from signal_engine.runtime.bootstrap import DEFAULT_GHOST_LOG, DEFAULT_PAPER_DIR  # noqa: E402

NY = ZoneInfo("America/New_York")
SESSIONS = (("Asia", "18:00", "03:00"), ("Europa", "03:00", "09:30"),
            ("Mañana NY", "09:30", "12:00"), ("Tarde NY", "12:00", "18:00"))


def session_of(iso: str | None) -> str:
    if not iso:
        return "?"
    hm = pd.Timestamp(iso).tz_convert(NY).strftime("%H:%M")
    for name, a, b in SESSIONS:
        if (a <= hm < b) if a < b else (hm >= a or hm < b):
            return name
    return "?"


def stats(rows: list[dict]) -> dict:
    v = [r["net_usd"] for r in rows]
    w = [x for x in v if x > 0]
    lo = [x for x in v if x < 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in v:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": len(v), "net": round(sum(v), 2), "won_pct": round(100 * len(w) / len(v), 1) if v else 0.0,
            "avg_win": round(sum(w) / len(w), 2) if w else 0.0,
            "avg_loss": round(sum(lo) / len(lo), 2) if lo else 0.0,
            "pf": round(sum(w) / -sum(lo), 2) if lo else (None if not w else float("inf")),
            "max_dd": round(dd, 2)}


def decisions(path: Path) -> dict:
    """Why GHOST (Set up 1) said no: count of each reason in its decision log."""
    c: Counter = Counter()
    if not path.exists():
        return {}
    with open(path) as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("action") == "NO_TRADE" and d.get("signal"):
                for r in d.get("no_trade_reasons") or []:
                    c[r] += 1
    return dict(c.most_common())


def main() -> None:
    cfg = EngineConfig.load_active()
    rt = SimpleNamespace(paper_tracker=PaperTradeTracker(store_dir=DEFAULT_PAPER_DIR),
                         live_feeds={"NQ": None}, shadows=None,
                         _point_values=lambda sym: {f"{cfg.ghost.contracts} MNQ": 2.0 * cfg.ghost.contracts})
    log = trade_log(rt)
    trades = log["trades"]
    for t in trades:
        t["session"] = session_of(t.get("opened_at"))
    by = defaultdict(list)
    for t in trades:
        by[t["src"]].append(t)

    report = []
    print(f"\nAUDITORÍA · {datetime.now(NY):%Y-%m-%d %H:%M} ET · cada set up por separado · $ con comisiones\n")
    print(f"{'Set up':34} {'trades':>6} {'gana%':>6} {'neto $':>10} {'PF':>5} {'peor racha $':>12}")
    for so in sorted((s for s in log["sources"] if s.get("setup")), key=lambda s: s["setup"]):
        rows = sorted(by.get(so["id"], []), key=lambda r: r.get("closed_at") or "")
        if so.get("kind") == "main":
            rows = [r for r in rows if r.get("version") == so.get("current")]
        s = stats(rows)
        name = so.get("setup_label") or so.get("name")
        pf = "—" if s["pf"] is None else ("∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}")
        print(f"{name[:34]:34} {s['n']:>6} {s['won_pct']:>6} {s['net']:>10,.0f} {pf:>5} {s['max_dd']:>12,.0f}")
        report.append({"setup": so["setup"], "name": name, "enabled": so.get("enabled", True),
                       "total": s,
                       "by_session": {k: stats([r for r in rows if r["session"] == k]) for k, *_ in SESSIONS},
                       "by_side": {k: stats([r for r in rows if r.get("side") == k]) for k in ("LONG", "SHORT")},
                       "by_exit": dict(Counter(r.get("why") or "?" for r in rows))})

    out = ROOT / f"auditoria-{datetime.now(NY):%Y-%m-%d}.json"
    clean_sources = [{k: v for k, v in s.items() if k in
                      ("id", "bot", "kind", "name", "setup", "setup_label", "about", "enabled",
                       "position", "fee", "current", "version")} for s in log["sources"]]
    out.write_text(json.dumps({"generated": datetime.now(NY).isoformat(), "report": report,
                               "ghost_no_trade_reasons": decisions(Path(DEFAULT_GHOST_LOG)),
                               "sources": clean_sources, "trades": trades}, default=str, indent=1))
    print(f"\nArchivo para la auditoría: {out}")
    print("Solo trades y nombres de set ups: sin webhooks, tokens ni contraseñas.\n")


if __name__ == "__main__":
    main()
