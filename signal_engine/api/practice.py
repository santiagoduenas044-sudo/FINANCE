"""A practice-only key, so a friend can use the Práctica screen (bar replay)
and nothing else: not the bots, not their trades, not the controls.

The owner creates it from the dashboard (Sistema). It lives in
data_store/practice_key, opens only the endpoints below and only for reading,
and a new one replaces the old (the old link stops working at once).
"""
from __future__ import annotations

import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEY_FILE = ROOT / "data_store" / "practice_key"

# what a practice key may open (GET only)
PRACTICE_PATHS = frozenset({"/api/replay/bars", "/api/practice/datasets"})
PAGE = "/practica"


class PracticeKey:
    def __init__(self, path: Path = KEY_FILE):
        self.path = Path(path)

    def get(self) -> str:
        try:
            return self.path.read_text().strip()
        except OSError:
            return ""

    def new(self) -> str:
        key = secrets.token_urlsafe(18)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(key)
        tmp.chmod(0o600)
        tmp.replace(self.path)
        return key

    def revoke(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def allows(self, token: str, method: str, path: str) -> bool:
        key = self.get()
        return bool(key) and bool(token) and secrets.compare_digest(token, key) \
            and method == "GET" and path in PRACTICE_PATHS


def write_env_value(env_file: Path, key: str, value: str) -> None:
    """Set KEY=value in the .env file run.sh loads at every start (replacing
    an existing KEY= line, even an empty one), keeping every other line."""
    env_file = Path(env_file)
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    out, done = [], False
    for line in lines:
        bare = line.strip()
        if bare.startswith("export "):
            bare = bare[len("export "):]
        if bare.split("=", 1)[0].strip() == key and "=" in bare:
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    tmp = env_file.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    tmp.chmod(0o600)
    tmp.replace(env_file)
