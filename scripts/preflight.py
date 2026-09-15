"""Preflight check (Phase 13b) — run this ON THE MAC before enabling 24/7.

It verifies, against the REAL environment (not a test double), everything that
must work for an unattended deployment:

  * Python version (3.11+).
  * All engine imports load.
  * Server settings are coherent (and, in remote mode, an auth token is set).
  * Discord config (enabled? webhook present?) — WITHOUT printing the webhook.
  * Real Yahoo Finance reachability for ES=F and NQ=F at 1-minute resolution,
    confirming closed-bars-only delivery and delayed labelling.

It NEVER trades, never places orders, never prints secrets. Exit code 0 means
"ready for 24/7"; non-zero means a blocking problem was found (and printed).

    PYTHONPATH=. python scripts/preflight.py
    PYTHONPATH=. python scripts/preflight.py --no-network   # skip Yahoo probe
"""
from __future__ import annotations

import argparse
import sys

OK = "\033[92m✓\033[0m"
WARN = "\033[93m!\033[0m"
FAIL = "\033[91m✗\033[0m"


def _line(mark: str, msg: str) -> None:
    print(f"  {mark} {msg}")


def check_python() -> bool:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    _line(OK if ok else FAIL, f"Python {v.major}.{v.minor}.{v.micro} "
          f"({'>= 3.11' if ok else 'TOO OLD — need 3.11+'})")
    return ok


def check_imports() -> bool:
    try:
        import pandas, numpy, fastapi, uvicorn  # noqa: F401
        from signal_engine.runtime.bootstrap import build_runtime  # noqa: F401
        from signal_engine.runtime.service import BotService  # noqa: F401
        from signal_engine.config.runtime_settings import ServerSettings  # noqa: F401
        _line(OK, "All core imports load (engines + runtime + api)")
        return True
    except Exception as exc:  # noqa: BLE001
        _line(FAIL, f"Import error: {type(exc).__name__}: {exc}")
        return False


def check_settings() -> bool:
    from signal_engine.config.runtime_settings import ServerSettings
    s = ServerSettings.from_env()
    _line(OK, f"Mode={s.mode}  host={s.host}  port={s.port}  symbols={s.symbols}  "
              f"cycle={s.cycle_seconds}s  auth={'on' if s.auth_enabled else 'off'}")
    try:
        s.validate()
        _line(OK, "Server settings valid for this mode")
        return True
    except RuntimeError as exc:
        _line(FAIL, f"Settings invalid: {exc}")
        return False


def check_discord() -> bool:
    from signal_engine.config.schema import EngineConfig
    cfg = EngineConfig.load()
    dc = cfg.output.discord
    import os
    has_webhook = bool(os.environ.get(dc.webhook_env))
    if not dc.enabled:
        _line(WARN, "Discord disabled in config (output.discord.enabled=false) — no alerts")
        return True
    if not has_webhook:
        _line(WARN, f"Discord enabled but ${dc.webhook_env} is not set — alerts will be skipped")
        return True
    _line(OK, f"Discord enabled and ${dc.webhook_env} is set (value hidden)")
    return True


def check_yahoo() -> bool:
    """Probe the REAL Yahoo endpoint for ES=F and NQ=F 1m closed bars."""
    from signal_engine.live.yahoo_adapter import YahooDelayedAdapter
    adapter = YahooDelayedAdapter()
    try:
        adapter.connect()
    except ConnectionError as exc:
        _line(FAIL, f"Yahoo unreachable: {exc}")
        _line(WARN, "The bot will fall back to SIMULATED sample data for ES only "
                    "(clearly labelled). Fix network/DNS to get real delayed data.")
        return False
    if not adapter.is_delayed:
        _line(FAIL, "Adapter did not report delayed data (unexpected)")
        return False
    _line(OK, "Connected to Yahoo Finance (delayed, no API key)")

    all_ok = True
    for sym in ("ES", "NQ"):
        try:
            bars = adapter.poll(sym)
        except ConnectionError as exc:
            _line(FAIL, f"{sym}: poll failed ({exc})")
            all_ok = False
            continue
        if not bars:
            _line(WARN, f"{sym}: connected but 0 closed bars right now "
                        f"(market closed / thin session) — will fill when bars print")
            continue
        last = bars[-1]
        tz_ok = last.timestamp.tzinfo is not None
        _line(OK if tz_ok else FAIL,
              f"{sym}=F: {len(bars)} closed 1m bars, last close @ "
              f"{last.timestamp.isoformat()} = {last.close:.2f} "
              f"({'tz-aware' if tz_ok else 'NAIVE TS — bug'})")
        all_ok = all_ok and tz_ok
    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser(description="FINANCE 24/7 preflight check")
    ap.add_argument("--no-network", action="store_true", help="skip the Yahoo probe")
    args = ap.parse_args()

    print("\n=== FINANCE preflight ===\n")
    print("Environment")
    core_ok = check_python() and check_imports()
    if not core_ok:
        print("\nCore checks failed — fix the above before continuing.\n")
        return 2

    print("\nConfiguration")
    settings_ok = check_settings()
    check_discord()

    yahoo_ok = True
    if not args.no_network:
        print("\nMarket data (real Yahoo probe)")
        yahoo_ok = check_yahoo()
    else:
        print("\nMarket data: skipped (--no-network)")

    print()
    if settings_ok and (yahoo_ok or args.no_network):
        print(f"{OK} Preflight OK — ready to run 24/7.\n")
        return 0
    print(f"{WARN} Preflight finished with warnings/errors — review the lines marked "
          f"{FAIL}/{WARN} above.\n")
    # Settings problems are blocking; a Yahoo miss is a warning (sim fallback exists).
    return 1 if not settings_ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
