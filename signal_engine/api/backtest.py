"""Backtests launched from the dashboard.

Runs scripts/audit_real_data.py — the same replay the operator runs in the
Terminal, through the real bot pipeline — as a separate low-priority process,
so a long run never slows the live bot's cycle. One run at a time. Results are
JSON files in data_store/backtests/, listed and read back by the dashboard.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ..config.schema import STRATEGIES_DIR

_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYMBOLS = ("NQ", "ES")

# What each strategy does, in the operator's words, for the backtest screen.
STRATEGY_INFO = {
    "zones": ("Zonas (estilo TradingMind) — la que usa el bot en vivo",
              "Marca huecos de precio (FVG) frescos en velas de 5 min. Avisa 🟡 cuando el "
              "precio se acerca y solo entra en el PRIMER regreso a la zona si una vela de "
              "1 min reacciona ahí. Stop 20–35 pts detrás del origen del movimiento; cierra "
              "la mitad en TP1 (1R) y pasa a breakeven; el resto va al máximo/mínimo sin "
              "tomar más cercano (2–5R). Entradas 9:30–15:30 ET, todo cerrado a las 16:55. "
              "Una operación a la vez."),
    "legacy": ("Setups antiguos (5 min) — la estrategia anterior del bot",
               "Tres setups en velas de 5 min: ruptura del rango de apertura (ORB), barrida "
               "de liquidez (toma los stops de un máximo/mínimo y rechaza) y recuperación "
               "del VWAP. Un filtro de confluencia (tendencia, estructura, VWAP, volumen) "
               "decide si opera. Objetivos 1R y 2R."),
}


class BacktestError(ValueError):
    """A request the dashboard should show back to the operator."""


class BacktestManager:
    def __init__(self, root: Path, python: str | None = None):
        self.root = Path(root)
        self.dir = self.root / "data_store" / "backtests"
        self.python = python or sys.executable
        self.proc: subprocess.Popen | None = None
        self.current: dict | None = None

    # ------------------------------------------------------------------ #
    def strategies(self) -> list[str]:
        return sorted(p.stem for p in STRATEGIES_DIR.glob("*.yaml")) + ["legacy"]

    def datasets(self) -> list[dict]:
        """1-minute CSVs under market_data/<SYMBOL>/<SOURCE>_1m.csv with their
        date range (first and last line only — the file can be 100 MB)."""
        out = []
        for sym in _SYMBOLS:
            for f in sorted((self.root / "market_data" / sym).glob("*_1m.csv")):
                first, last = _first_last_dates(f)
                if first and last:
                    out.append({"symbol": sym, "data": f.name[:-len("_1m.csv")],
                                "first": first, "last": last,
                                "size_mb": round(f.stat().st_size / 1e6, 1)})
        return out

    # ------------------------------------------------------------------ #
    def start(self, *, symbol: str, data: str, start: str, end: str,
              strategy: str) -> dict:
        if self.running():
            raise BacktestError("Ya hay un backtest corriendo; espera o cancélalo.")
        if symbol not in _SYMBOLS:
            raise BacktestError(f"Símbolo no válido: {symbol}")
        if not _ID.match(data or ""):
            raise BacktestError("Fuente de datos no válida.")
        if not (_DATE.match(start or "") and _DATE.match(end or "")) or start > end:
            raise BacktestError("Fechas no válidas (AAAA-MM-DD, desde ≤ hasta).")
        if strategy not in self.strategies():
            raise BacktestError(f"Estrategia desconocida: {strategy}")
        if not (self.root / "market_data" / symbol / f"{data}_1m.csv").exists():
            raise BacktestError(f"No hay datos {data} de {symbol}; descárgalos primero.")

        run_id = time.strftime("%Y%m%d-%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        out, prog, logf = (self.dir / f"{run_id}.json", self.dir / f"{run_id}.progress.json",
                           self.dir / f"{run_id}.log")
        cmd = [self.python, "scripts/audit_real_data.py", "--offline",
               "--csv-root", str(self.root / "market_data"), "--symbol", symbol,
               "--contract", data, "--start", start, "--end", end, "--strategy", strategy,
               "--json-out", str(out), "--progress-file", str(prog)]
        env = {**os.environ, "PYTHONPATH": str(self.root)}
        with open(logf, "w") as log:
            self.proc = subprocess.Popen(
                cmd, cwd=self.root, env=env, stdout=log, stderr=subprocess.STDOUT,
                # lower priority: the live bot's cycle always wins the CPU
                preexec_fn=(lambda: os.nice(10)) if hasattr(os, "nice") else None)
        self.current = {"id": run_id, "symbol": symbol, "data": data, "start": start,
                        "end": end, "strategy": strategy, "started": time.time()}
        return self.status()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def cancel(self) -> dict:
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return self.status()

    def status(self) -> dict:
        if self.current is None:
            return {"state": "idle"}
        cur = dict(self.current)
        prog = self.dir / f"{cur['id']}.progress.json"
        try:
            cur["progress"] = json.loads(prog.read_text())
        except (OSError, ValueError):
            cur["progress"] = None
        cur["elapsed"] = round(time.time() - cur["started"])
        if self.running():
            cur["state"] = "running"
        elif (self.dir / f"{cur['id']}.json").exists():
            cur["state"] = "done"
        else:
            cur["state"] = "failed"
            cur["log_tail"] = _tail(self.dir / f"{cur['id']}.log", 15)
        return cur

    # ------------------------------------------------------------------ #
    def runs(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.json"), reverse=True) if self.dir.exists() else []:
            if f.name.endswith(".progress.json"):
                continue
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            out.append({"id": f.stem, "meta": d.get("meta"), "total": d.get("total")})
        return out

    def get(self, run_id: str) -> dict:
        if not _ID.match(run_id or ""):
            raise BacktestError("id no válido")
        f = self.dir / f"{run_id}.json"
        if not f.exists():
            raise BacktestError("no existe ese backtest")
        return json.loads(f.read_text())


def _first_last_dates(path: Path) -> tuple[str | None, str | None]:
    try:
        with open(path, "rb") as fh:
            fh.readline()                               # header
            first = fh.readline().decode(errors="ignore")
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            last = fh.read().decode(errors="ignore").strip().splitlines()[-1]
        return first[:10] or None, last[:10] or None
    except (OSError, IndexError):
        return None, None


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(errors="ignore").splitlines()[-n:]
    except OSError:
        return []
