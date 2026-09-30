"""Check GHOST on real market days, without touching its real record.

    .venv/bin/python -m signal_engine.runtime.probar               # Yahoo, last days
    .venv/bin/python -m signal_engine.runtime.probar --desde 2026-09-01 [--hasta 2026-09-26]
    .venv/bin/python -m signal_engine.runtime.probar --log [DIAS]  # what the live GHOST decided

The replay feeds the 1-minute bars, one at a time, through the same runtime
the live bot runs (setups -> signal -> GHOST -> paper tracker) with today's
config, in a temporary folder, with Discord off. Every trade idea is listed
with what GHOST did with it: taken, or the reason it was turned down.

`--log` reads data_store/signal_log.jsonl instead: what the live bot actually
did with each idea, with its reasons.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timezone
from pathlib import Path

import pandas as pd

from ..config.schema import EngineConfig
from ..data.models import tf_to_timedelta
from ..data.resample import resample_ohlcv
from ..output.dispatcher import OutputDispatcher
from ..output.record import won_lost
from .engine import _IDEA_REJECT_ES

ROOT = Path(__file__).resolve().parents[2]
SIGNAL_LOG = ROOT / "data_store" / "signal_log.jsonl"
SYMBOL = "NQ"
WINDOW = 1440                # 1m bars the live feed hands the bot (24h)

REASONS_ES = {
    **_IDEA_REJECT_ES,
    "duplicate": "ya la había evaluado",
    "too_late_in_session": "después de la hora límite de entrada",
    "stop_too_tight": "stop demasiado corto",
    "stop_too_wide": "stop demasiado largo",
    "insufficient_rr": "poca ganancia para el riesgo",
    "target_too_close": "objetivo demasiado cerca",
    "conflicting_setups": "señales en ambas direcciones a la vez",
    "conflicting_timeframe": "las temporalidades mayores van en contra",
    "against_structure": "va contra la estructura",
    "insufficient_confluence": "poca confluencia",
    "setup_incomplete": "la señal no estaba completa",
    "invalid_trade_plan": "plan de trade inválido",
    "risk_rejected": "el control de riesgo la bloqueó",
    "daily_limit": "ya se alcanzó el límite del día (pérdida máxima)",
    "news_event": "había una noticia fuerte en ese momento",
}


def reasons_es(codes: list[str]) -> str:
    return " · ".join(REASONS_ES.get(c, c) for c in codes) or "sin motivo registrado"


def _hm(ts) -> str:
    return pd.Timestamp(ts).tz_convert("America/New_York").strftime("%d-%b %H:%M")


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
def replay(bars: pd.DataFrame, cfg: EngineConfig | None = None,
           data_dir: Path | None = None, first: str = "09:31", last: str = "16:56"):
    """Run CLOSE-labeled ET 1m `bars` through the live runtime, one minute at a
    time, between `first` and `last` ET (GHOST trades the day session only;
    the bars before still build the zones). Returns (runtime, idea rows of
    its signal log)."""
    from .bootstrap import build_runtime
    from .models import BotStatus

    cfg = cfg or EngineConfig.load_active()
    bars = bars.copy()
    if "volume" not in bars.columns:
        bars["volume"] = 0.0
    tmp = None
    if data_dir is None:
        tmp = tempfile.TemporaryDirectory()
        data_dir = Path(tmp.name)
    rt, _ = build_runtime([], config=cfg, signal_log_path=data_dir / "signal_log.jsonl",
                          data_dir=data_dir)
    rt.output_dispatcher = OutputDispatcher([])          # never Discord from a replay
    clock = {"now": None}
    rt.now_fn = lambda: clock["now"]
    rt.status = BotStatus.ONLINE

    de = rt.data_engine
    base_tf = cfg.data.base_timeframe
    frames = {base_tf: de.classifier.annotate(bars, label_interval=tf_to_timedelta(base_tf))}
    for tf in cfg.data.target_timeframes:
        if tf != base_tf:
            # resampling the whole history once is the same as the live feed
            # resampling its buffer every minute: the as-of cut below drops
            # the still-forming bar exactly as the live context does
            frames[tf] = resample_ohlcv(frames[base_tf], tf, cfg.sessions, cfg.resample,
                                        cfg.data.timezone)
    keep = {tf: max(1, int(WINDOW * tf_to_timedelta(base_tf) / tf_to_timedelta(tf)))
            for tf in frames}
    times = frames[base_tf].index
    hm = times.strftime("%H:%M")
    for i in range(len(times)):
        if not (first <= hm[i] <= last):
            continue
        t = times[i]
        tf_bars = {}
        for tf, df in frames.items():
            k = int(df.index.searchsorted(t, side="right"))
            tf_bars[tf] = df.iloc[max(0, k - keep[tf]):k]
        clock["now"] = t.to_pydatetime().astimezone(timezone.utc)
        rt.run_cycle(SYMBOL, tf_bars)

    rows = _read_rows(data_dir / "signal_log.jsonl")
    if tmp is not None:
        rt._probar_tmp = tmp          # keep the folder alive while `rt` is used
    return rt, rows


def usd_per_point(cfg: EngineConfig) -> float:
    g = cfg.ghost
    return g.contracts * (2.0 if g.trade_micro else 20.0)


def _read_rows(path: Path) -> list[dict]:
    """Signal-log rows of the trade ideas (one per idea: a SIGNAL when GHOST
    took it, a NO_TRADE with its reasons when not)."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("timestamp") and d.get("setup") and d.get("reasons") != ["duplicate"]:
            rows.append(d)
    return rows


def idea_lines(rows: list[dict]) -> list[str]:
    out = []
    for d in rows:
        if d["kind"] == "SIGNAL":
            tp = (d.get("take_profits") or [None])[-1]
            out.append(f"  {_hm(d['timestamp'])}  {d['direction']:<5} → TOMADO  entrada "
                       f"{d['entry']:,.2f} · stop {d['stop']:,.2f}"
                       + (f" · objetivo {tp:,.2f}" if tp is not None else ""))
        else:
            side = d.get("candidate_direction") or "idea"
            out.append(f"  {_hm(d['timestamp'])}  {side:<5} → descartada: "
                       f"{reasons_es(d.get('reasons') or [])}")
    return out


def report(rt, rows: list[dict], cfg: EngineConfig) -> str:
    pv = usd_per_point(cfg)
    out = ["Ideas que GHOST evaluó:"]
    out += idea_lines(rows) or ["  (ninguna: no se formó ni confirmó ninguna zona en esos días)"]
    pt = rt.paper_tracker
    trades = sorted(list(pt.closed_trades) + [t for t in pt.open_trades if t.is_open],
                    key=lambda t: t.opened_at)
    out.append("")
    out.append(f"Trades ({cfg.ghost.contracts} {'MNQ' if cfg.ghost.trade_micro else 'NQ'}, "
               "sin comisiones):")
    if not trades:
        out.append("  ninguno")
    for t in trades:
        how = t.exit_reason.value.lower() if t.exit_reason else "abierto"
        px = t.exit_price if t.exit_price is not None else t.current_price
        out.append(f"  {_hm(t.opened_at)}  {t.direction:<5} {t.entry:,.2f} → {px:,.2f} "
                   f"({how})  {t.pnl_points:+.2f} pts  "
                   f"{'+' if t.pnl_points >= 0 else '−'}${abs(t.pnl_points) * pv:,.0f}")
    if trades:
        out.append("  " + won_lost([t.pnl_points for t in trades], pv).replace("**", ""))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# the live log
# --------------------------------------------------------------------------- #
def read_log(days: int = 3, path: Path = SIGNAL_LOG) -> str:
    """What the live GHOST did with each idea on the last `days` days."""
    rows = _read_rows(path)
    if not rows:
        return "El registro de señales está vacío: GHOST no ha evaluado ninguna idea todavía."
    day = lambda d: pd.Timestamp(d["timestamp"]).tz_convert("America/New_York").date()  # noqa: E731
    wanted = sorted({day(d) for d in rows})[-days:]
    rows = [d for d in rows if day(d) in wanted]
    out = [f"Lo que GHOST hizo de verdad ({wanted[0]} → {wanted[-1]}):"] + idea_lines(rows)
    counts: dict[str, int] = {}
    for d in rows:
        for c in (d.get("reasons") or []) if d["kind"] != "SIGNAL" else []:
            counts[c] = counts.get(c, 0) + 1
    if counts:
        out.append("")
        out.append("Motivos de descarte: " + " · ".join(
            f"{REASONS_ES.get(k, k)} ×{n}" for k, n in sorted(counts.items(), key=lambda x: -x[1])))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
def _arg(name: str) -> str | None:
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


def main() -> int:
    import logging
    logging.disable(logging.WARNING)
    if "--log" in sys.argv:
        n = _arg("--log")
        print(read_log(int(n) if n and n.isdigit() else 3))
        return 0
    from ..subbots.orbit import databento_bars, yahoo_bars
    desde, hasta = _arg("--desde"), _arg("--hasta")
    if desde:
        print(f"Leyendo el historial de Databento desde {desde}...", flush=True)
        start = str((pd.Timestamp(desde) - pd.Timedelta(days=3)).date())   # zones need the days before
        bars = databento_bars(start, hasta, hours=None)
    else:
        print("Bajando las velas de 1 min de NQ de los últimos días (Yahoo)...", flush=True)
        bars = yahoo_bars()
    if bars.empty:
        print("No hay velas en ese rango.")
        return 1
    cfg = EngineConfig.load_active()
    days = bars.index.normalize().nunique()
    print(f"{len(bars):,} velas ({days} días). Repasando minuto a minuto con las reglas de hoy "
          f"(estrategia {cfg.setups.zone_retrace.timeframe} zonas)...\n", flush=True)
    rt, rows = replay(bars, cfg)
    if desde:
        cut = pd.Timestamp(desde, tz="America/New_York")
        rows = [d for d in rows if pd.Timestamp(d["timestamp"]) >= cut]
    print(report(rt, rows, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
