"""Strategy lab launched from the dashboard (Backtest view).

Runs scripts/strategy_lab.py as a separate low-priority process — it takes a
few minutes — and keeps its printed report in data_store/lab/<time>.txt, so
the phone can start it and read the result without a Terminal.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

POSITIONS = ("5mnq", "1nq", "riesgo")
SESSIONS = ("rth", "noche", "all")
STUDIES = ("todo", "apertura", "zonas", "proteccion")
SCALES = ("puntos", "precio")


class LabError(ValueError):
    """A request the dashboard should show back to the operator."""


class LabManager:
    def __init__(self, root: Path, python: str | None = None):
        self.root = Path(root)
        self.dir = self.root / "data_store" / "lab"
        self.python = python or sys.executable
        self.proc: subprocess.Popen | None = None
        self.current: dict | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, *, position: str = "5mnq", sessions: str = "rth",
              estudio: str = "todo", escala: str = "puntos") -> dict:
        if self.running():
            raise LabError("El laboratorio ya está corriendo; espera a que termine.")
        if (position not in POSITIONS or sessions not in SESSIONS or estudio not in STUDIES
                or escala not in SCALES):
            raise LabError("Opciones no válidas.")
        self.dir.mkdir(parents=True, exist_ok=True)
        run_id = time.strftime("%Y%m%d-%H%M%S")
        out = self.dir / f"{run_id}.txt"
        env = {**os.environ, "PYTHONPATH": str(self.root)}
        with open(out, "w") as fh:
            self.proc = subprocess.Popen(
                [self.python, "scripts/strategy_lab.py", "--position", position,
                 "--sessions", sessions, "--estudio", estudio, "--escala", escala],
                cwd=self.root, env=env, stdout=fh, stderr=subprocess.STDOUT,
                preexec_fn=(lambda: os.nice(10)) if hasattr(os, "nice") else None)
        self.current = {"id": run_id, "position": position, "sessions": sessions,
                        "estudio": estudio, "escala": escala,
                        "started": time.time()}
        return self.status()

    def status(self) -> dict:
        cur = self.current
        if cur is None:                                   # after a restart: the last report
            files = sorted(self.dir.glob("*.txt")) if self.dir.exists() else []
            if not files:
                return {"state": "idle"}
            cur = {"id": files[-1].stem}
        path = self.dir / f"{cur['id']}.txt"
        try:
            text = path.read_text(errors="ignore")[-20000:]
        except OSError:
            text = ""
        state = "running" if self.running() and self.current is cur else "done"
        if state == "done" and self.proc is not None and self.current is cur \
                and self.proc.returncode not in (0, None):
            state = "failed"
        return {**cur, "state": state, "text": text,
                "elapsed": round(time.time() - cur["started"]) if "started" in cur else None}
