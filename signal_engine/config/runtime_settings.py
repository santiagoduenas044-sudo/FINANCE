"""Runtime / server settings (Phase 13) — environment-driven configuration for
running the bot and dashboard unattended (24/7) and, later, remotely.

These settings are intentionally SEPARATE from ``EngineConfig`` (the trading
config): they control *how the process runs and is served*, never *what the bot
decides*. Nothing here touches the Data / Market Analysis / Setup / Signal /
Risk engines.

Everything has a safe, local-only default. Reading the real values from the
environment (never hard-coded secrets) keeps the same code working on a laptop
(local mode) and on a VPS (remote/server mode).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get(name: str, default: str) -> str:
    val = os.environ.get(name)
    return default if val is None or val == "" else val


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _get_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class ServerSettings:
    """How the dashboard/API process is served and secured.

    Modes:
      * ``local``  — bind 127.0.0.1 (loopback only). The safe default; the
        dashboard is reachable only from the same machine.
      * ``remote`` — bind the configured host (e.g. 0.0.0.0) so a VPS can serve
        it. In remote mode an auth token is REQUIRED (the app refuses to start
        an unauthenticated public server) and CORS is locked to explicit
        origins.
    """
    mode: str = "local"
    host: str = "127.0.0.1"
    port: int = 8000
    # Trading symbols to run. Comma-separated env, e.g. "ES,NQ".
    symbols: list[str] = field(default_factory=lambda: ["ES", "NQ"])
    # Seconds between poll/cycle ticks in the runtime loop.
    cycle_seconds: float = 5.0
    # Start the trading pipeline automatically on boot (headless 24/7 use).
    auto_start: bool = False
    # --- security (placeholders; NO secrets are invented or stored here) --- #
    # Name of the env var that holds the dashboard bearer token. The token
    # value itself is NEVER stored in code or committed — same pattern as the
    # Discord webhook. Empty token => auth disabled (fine for local mode).
    auth_token_env: str = "DASHBOARD_TOKEN"
    # Explicit CORS allow-list (only applied when non-empty). Never "*".
    cors_origins: list[str] = field(default_factory=list)
    # Where to write the unattended log (empty => stdout only).
    log_file: str = ""

    @property
    def auth_token(self) -> str:
        """The configured bearer token, read live from the environment (may be
        empty). Never logged, never included in any API response."""
        return os.environ.get(self.auth_token_env, "").strip()

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_token)

    @property
    def is_remote(self) -> bool:
        return self.mode == "remote"

    def validate(self) -> None:
        """Fail fast on an unsafe remote configuration. Called by the servers
        before binding — never let a public server come up with no auth."""
        if self.is_remote and not self.auth_enabled:
            raise RuntimeError(
                f"remote mode requires an auth token: set ${self.auth_token_env} "
                f"(the dashboard refuses to serve publicly without authentication)."
            )
        if "*" in self.cors_origins:
            raise RuntimeError("CORS wildcard '*' is not allowed; list explicit origins.")

    @classmethod
    def from_env(cls) -> "ServerSettings":
        mode = _get("BOT_MODE", "local").strip().lower()
        # Sensible host default per mode: loopback for local, all-interfaces for
        # remote (behind the operator's own firewall / reverse proxy).
        default_host = "0.0.0.0" if mode == "remote" else "127.0.0.1"
        return cls(
            mode=mode,
            host=_get("BOT_HOST", default_host),
            port=int(_get_float("BOT_PORT", 8000)),
            symbols=_get_list("BOT_SYMBOLS", ["ES", "NQ"]),
            cycle_seconds=_get_float("BOT_CYCLE_SECONDS", 5.0),
            # Canonical: BOT_AUTO_START. BOT_AUTOSTART is accepted as an alias
            # (BOT_AUTO_START wins if both are set). An unset/invalid value is a
            # safe False — a misconfigured flag never forces trading on.
            auto_start=_get_bool("BOT_AUTO_START", _get_bool("BOT_AUTOSTART", False)),
            auth_token_env=_get("DASHBOARD_TOKEN_ENV", "DASHBOARD_TOKEN"),
            cors_origins=_get_list("BOT_CORS_ORIGINS", []),
            log_file=_get("BOT_LOG_FILE", ""),
        )

    def public_dict(self) -> dict:
        """JSON-safe, secret-free view for diagnostics/health — never the token."""
        return {
            "mode": self.mode,
            "host": self.host,
            "port": self.port,
            "symbols": list(self.symbols),
            "cycle_seconds": self.cycle_seconds,
            "auto_start": self.auto_start,
            "auth_enabled": self.auth_enabled,
            "cors_origins": list(self.cors_origins),
        }
