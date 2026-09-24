"""Re-run a closed paper trade under the current rules and correct the record.

For when a rule was wrong at the time — e.g. idea #4 was force-closed at 4:00pm
while NQ kept trading and the move paid after that. The trade is replayed on
the real 1-minute bars with the SAME entry, stop and targets, through the same
paper tracker and end-of-day rule the bot runs now. Nothing is invented: the
new result is whatever those bars produce.

    # 1) see the old vs corrected result (changes nothing)
    PYTHONPATH=. .venv/bin/python scripts/correct_trade.py --idea 4
    # 2) stop the bot, write the correction + post it to Discord with a chart
    bash deploy/macos/ctl.sh stop
    PYTHONPATH=. .venv/bin/python scripts/correct_trade.py --idea 4 --apply --discord
    bash deploy/macos/ctl.sh start

The previous record is backed up to data_store/archive/ before it is changed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signal_engine.config.schema import EngineConfig  # noqa: E402
from signal_engine.data.engine import DataEngine  # noqa: E402
from signal_engine.data.ingestion import normalize  # noqa: E402
from signal_engine.data.providers.csv_provider import CsvProvider  # noqa: E402
from signal_engine.paper.tracker import PaperTradeTracker  # noqa: E402
from signal_engine.runtime.engine import eod_due  # noqa: E402

CLOSED = ROOT / "data_store" / "paper" / "paper_closed.jsonl"
MARKET = ROOT / "market_data"


def _audit_module():
    spec = importlib.util.spec_from_file_location("audit_real_data",
                                                  ROOT / "scripts" / "audit_real_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def find_trade(rows: list[dict], idea: int | None, signal: str | None,
               day: str | None) -> dict:
    hits = []
    for r in rows:
        ctx = r.get("context") or {}
        if signal and not r["signal_id"].startswith(signal):
            continue
        if idea is not None and ctx.get("idea_number") != idea:
            continue
        if day and not str(r.get("opened_at", "")).startswith(day):
            continue
        hits.append(r)
    if not hits:
        raise SystemExit("No encontré ese trade en el récord.")
    return hits[-1]                      # the most recent match


def initial_stop(t: dict) -> float:
    risk = t["initial_risk"]
    return t["entry"] - risk if t["direction"] == "LONG" else t["entry"] + risk


def replay(t: dict, bars: pd.DataFrame, cfg: EngineConfig):
    """Same entry/stop/targets, today's tracker + end-of-day rule."""
    g = cfg.ghost
    de = DataEngine(cfg)
    tr = PaperTradeTracker(breakeven_after_tp1=g.breakeven_after_tp1,
                           partial_at_tp1=g.partial_at_tp1)
    opened = pd.Timestamp(t["opened_at"])
    trade = tr.open_trade(signal_id=t["signal_id"], instrument=t["instrument"],
                          direction=t["direction"], setup=t["setup"], entry=t["entry"],
                          stop=initial_stop(t), take_profits=list(t["take_profits"]),
                          opened_at=opened, invalidation_price=t.get("invalidation_price", 0.0),
                          timeframe=t.get("timeframe"), context=dict(t.get("context") or {}))
    for ts, bar in bars[bars.index > opened].iterrows():
        tr.update_on_bar(trade, bar, ts)
        if trade.is_open and g.eod_close_enabled and \
                eod_due(de.classifier.classify(ts), ts, g.eod_close_time):
            tr.close_symbol_eod(t["instrument"], float(bar["close"]), ts)
        if not trade.is_open:
            return trade
    return None                           # the data ends before the trade does


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    env = ROOT / ".env"
    if not val and env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith(name + "="):
                val = line.split("=", 1)[1].strip().strip("'\"")
    return val


_EXIT = {"TP1": "TP1", "TP2": "TP final", "STOP": "stop", "INVALIDATION": "invalidado",
         "EOD": "cierre fin de día"}


def _fmt(t: dict) -> str:
    reason = _EXIT.get(t.get("exit_reason"), t.get("exit_reason"))
    if t.get("exit_reason") == "STOP" and t.get("tp1_hit"):
        reason = "BE tras parcial en TP1" if t.get("partial_fraction") else "breakeven"
    closed = pd.Timestamp(t["closed_at"]).strftime("%H:%M") if t.get("closed_at") else "?"
    return (f"{reason} {closed} en {t.get('exit_price'):,.2f} · "
            f"{t.get('pnl_points', 0):+.2f} pts ({t.get('r_multiple', 0):+.2f}R)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--idea", type=int, help="número de idea (p.ej. 4)")
    ap.add_argument("--signal", help="inicio del signal_id, si no hay número de idea")
    ap.add_argument("--date", help="YYYY-MM-DD del trade (si el número se repite)")
    ap.add_argument("--offline", action="store_true", help="usar el CSV de Yahoo ya bajado")
    ap.add_argument("--apply", action="store_true", help="corregir el récord")
    ap.add_argument("--discord", action="store_true", help="mandar la corrección con gráfica")
    a = ap.parse_args()
    if a.idea is None and not a.signal:
        raise SystemExit("Indica --idea N o --signal <id>.")

    rows = [json.loads(x) for x in CLOSED.read_text().splitlines() if x.strip()]
    old = find_trade(rows, a.idea, a.signal, a.date)
    sym = old["instrument"]
    cfg = EngineConfig.load_active()
    audit = _audit_module()
    if not a.offline:
        print(f"Bajando velas de 1m de {sym} (Yahoo, últimos 7 días)...")
        audit.download(sym, 7)
    raw = CsvProvider(MARKET).get_raw_bars(sym, "1m", contract=audit.CONTRACT)
    bars = normalize(raw, cfg.data, "1m")

    new = replay(old, bars, cfg)
    if new is None:
        raise SystemExit("Las velas descargadas no cubren el trade completo; "
                         "no corrijo nada.")
    nd = new.to_dict()
    name = f"IDEA #{(old.get('context') or {}).get('idea_number', '?')}"
    print(f"{name} {old['direction']} {sym} · entrada {old['entry']:,.2f}")
    print(f"  antes:     {_fmt(old)}")
    print(f"  corregido: {_fmt(nd)}")
    if (nd["exit_reason"], round(nd["pnl_points"], 2)) == \
            (old.get("exit_reason"), round(old.get("pnl_points", 0), 2)):
        print("Mismo resultado con las reglas actuales: no hay nada que corregir.")
        return 0

    if a.apply:
        nd["context"] = {**(old.get("context") or {}), "correction": {
            "old_exit_reason": old.get("exit_reason"), "old_exit_price": old.get("exit_price"),
            "old_pnl_points": old.get("pnl_points"), "old_closed_at": old.get("closed_at"),
            "corrected_at": datetime.now().isoformat(timespec="seconds"),
            "reason": f"cierre de fin de día movido a {cfg.ghost.eod_close_time} ET"}}
        backup = ROOT / "data_store" / "archive" / \
            f"{datetime.now():%Y%m%d-%H%M%S}-paper_closed.jsonl"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CLOSED, backup)
        CLOSED.write_text("".join(json.dumps(nd if r["signal_id"] == old["signal_id"] else r)
                                  + "\n" for r in rows))
        print(f"Récord corregido (copia del anterior en {backup.relative_to(ROOT)}).")
        print("Reinicia el bot para que el dashboard y !status lo lean.")

    if a.discord:
        post_correction(old, nd, bars, name, cfg)
    return 0


def post_correction(old: dict, nd: dict, bars: pd.DataFrame, name: str, cfg) -> None:
    from signal_engine.output.discord import send_webhook_with_file
    from signal_engine.output.trade_chart import render_trade_png

    url = _env(cfg.output.discord.webhook_env)
    if not url:
        raise SystemExit(f"Falta {cfg.output.discord.webhook_env} en .env; no mandé nada.")
    opened = pd.Timestamp(old["opened_at"])
    closed = pd.Timestamp(nd["closed_at"])
    window = bars[(bars.index >= opened - pd.Timedelta(minutes=30))
                  & (bars.index <= closed + pd.Timedelta(minutes=15))]
    marks = [(opened, old["entry"], "Entrada", "#bbbbbb"),
             (pd.Timestamp(old["closed_at"]), old["exit_price"], "Antes", "#9e9e9e"),
             (closed, nd["exit_price"], "Corregido", "#26a69a")]
    png = render_trade_png(window, {**nd, "stop_initial": initial_stop(old)},
                           title=f"{name} · {old['instrument']} 1m · corrección", marks=marks)
    pts = nd["pnl_points"]
    mark = "✅" if pts > 0 else ("➖" if pts == 0 else "🛑")
    usd = " · ".join(f"{'+' if pts >= 0 else '−'}${abs(pts) * pv:,.0f} {s}"
                     for s, pv in sorted(((nd.get("context") or {}).get("usd_per_point")
                                          or {}).items(), key=lambda kv: kv[1]))
    payload = {"embeds": [{
        "title": f"🛠️ CORRECCIÓN · {name} — ahora {mark} {pts:+.2f} pts",
        "description": (f"Antes: {_fmt(old)}\n"
                        f"Corregido: {_fmt(nd)}" + (f" · {usd}" if usd else "") + "\n"
                        f"Motivo: el cierre de fin de día pasa a las "
                        f"{cfg.ghost.eod_close_time} ET (NQ opera hasta las 5pm; "
                        f"Apex pide cerrar antes de 4:59pm)."),
        "color": 0x00C851 if pts > 0 else 0xFF4444,
        "image": {"url": "attachment://correccion.png"},
        "footer": {"text": "Récord corregido con las velas reales de 1m."}}]}
    status = send_webhook_with_file(url, payload, png, "correccion.png")
    print("Corrección enviada a Discord." if 200 <= status < 300
          else f"Discord respondió HTTP {status}; revisa el webhook.")


if __name__ == "__main__":
    sys.exit(main())
