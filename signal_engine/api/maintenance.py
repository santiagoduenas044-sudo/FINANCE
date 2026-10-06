"""Keep the bot up to date from the phone: pull the latest code, restart,
and read a backtest's reports — the things that used to need the Terminal.

Update = `git pull --ff-only` of the branch the Mac is on (never a merge,
never a reset, never another branch); dependencies are reinstalled only when
requirements.txt changed. Restart = the process exits and launchd (KeepAlive)
starts it again with the new code. Everything here sits behind the same
bearer token as the rest of the API.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_REPORTS = {"summary": ("scripts/backtest_summary.py", ".json"),
            "filters": ("scripts/edge_finder.py", ".shadow.csv")}


class MaintenanceError(ValueError):
    """A request the dashboard should show back to the operator."""


def _git(root: Path, *args: str, timeout: float = 60) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                         timeout=timeout)
    if out.returncode != 0:
        raise MaintenanceError((out.stderr or out.stdout).strip()[-600:] or "git falló")
    return out.stdout.strip()


def version(root: Path) -> dict:
    try:
        return {"branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
                "commit": _git(root, "rev-parse", "--short", "HEAD"),
                "subject": _git(root, "log", "-1", "--format=%s"),
                "date": _git(root, "log", "-1", "--format=%cI")}
    except (MaintenanceError, OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)[:300]}


def update(root: Path, python: str | None = None) -> dict:
    """Fast-forward the current branch from origin. Returns what changed."""
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise MaintenanceError("La Mac no está en una rama (HEAD suelto); actualiza a mano.")
    before = _git(root, "rev-parse", "HEAD")
    _git(root, "fetch", "origin", branch, timeout=120)
    _git(root, "merge", "--ff-only", "FETCH_HEAD", timeout=60)
    after = _git(root, "rev-parse", "HEAD")
    changes = _git(root, "log", "--format=%h %s", f"{before}..{after}").splitlines() \
        if before != after else []
    files = _git(root, "diff", "--name-only", before, after).splitlines() \
        if before != after else []
    deps = None
    if "requirements.txt" in files:
        pip = subprocess.run([python or sys.executable, "-m", "pip", "install", "-q", "-r",
                              "requirements.txt"], cwd=root, capture_output=True, text=True,
                             timeout=600)
        deps = "ok" if pip.returncode == 0 else (pip.stderr or pip.stdout)[-600:]
    return {"branch": branch, "before": before[:7], "after": after[:7],
            "updated": before != after, "commits": changes[:30], "files": len(files),
            "dependencies": deps, "orbit": restart_orbit() if before != after else None}


ORBIT_RESTART_FLAG = Path(__file__).resolve().parents[2] / "data_store" / "orbit.restart"


def restart_orbit() -> str:
    """ORBIT (the separate APERTURA paper bot) runs as its own launchd
    service: restart it too so it picks up the new code. Nothing to do when
    it is not installed. In the cloud (Docker) it is told through a file:
    ORBIT sees it within a second, exits, and Docker starts it again."""
    if os.environ.get("BOT_IN_DOCKER"):
        ORBIT_RESTART_FLAG.parent.mkdir(parents=True, exist_ok=True)
        ORBIT_RESTART_FLAG.touch()
        return "se reinicia en unos segundos"
    label = f"gui/{os.getuid()}/com.finance.orbit" if hasattr(os, "getuid") else ""
    try:
        if not label or subprocess.run(["launchctl", "print", label], capture_output=True,
                                       timeout=10).returncode != 0:
            return "no está encendido"
        subprocess.run(["launchctl", "kickstart", "-k", label], capture_output=True, timeout=20)
        return "reiniciado"
    except (OSError, subprocess.TimeoutExpired):
        return "no está encendido"


def schedule_restart(delay: float = 1.5, exit_fn=os._exit) -> None:
    """Exit shortly after the HTTP response is sent; launchd (KeepAlive)
    starts the bot again with whatever code is on disk now."""
    threading.Timer(delay, lambda: exit_fn(0)).start()


def report(root: Path, run_id: str, kind: str, python: str | None = None) -> str:
    """Text of scripts/backtest_summary.py or scripts/edge_finder.py for a
    saved backtest, as the Terminal would print it."""
    if kind not in _REPORTS:
        raise MaintenanceError("reporte desconocido")
    if not _ID.match(run_id or ""):
        raise MaintenanceError("id no válido")
    script, suffix = _REPORTS[kind]
    path = root / "data_store" / "backtests" / f"{run_id}{suffix}"
    if not path.exists():
        raise MaintenanceError("Ese backtest no tiene ese archivo (¿es de una versión anterior?).")
    out = subprocess.run([python or sys.executable, script, str(path)], cwd=root,
                         capture_output=True, text=True, timeout=300,
                         env={**os.environ, "PYTHONPATH": str(root)})
    return (out.stdout + (("\n" + out.stderr[-1500:]) if out.returncode else "")).strip()
