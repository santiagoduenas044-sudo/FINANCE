"""Tests for the macOS 24/7 deployment artifacts (Phase 13b).

These validate the deployment tooling itself — the preflight diagnostic and the
launchd plist template — without needing a Mac or real network. They confirm the
things a broken deploy would trip on: the plist parses and carries the
auto-start/auto-restart/graceful-shutdown keys, and the preflight's offline
checks are correct.
"""
import plistlib
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MACOS = ROOT / "deploy" / "macos"


# --------------------------------------------------------------------------- #
# launchd plist template
# --------------------------------------------------------------------------- #
def test_plist_template_renders_to_valid_plist():
    tmpl = (MACOS / "com.finance.signal.plist.template").read_text()
    rendered = (tmpl.replace("__REPO__", "/Users/me/FINANCE")
                    .replace("__LOGDIR__", "/Users/me/Library/Logs/finance-signal"))
    d = plistlib.loads(rendered.encode("utf-8"))
    assert d["Label"] == "com.finance.signal"
    assert d["RunAtLoad"] is True          # auto-start on login
    assert d["KeepAlive"] is True          # auto-restart on failure
    assert d["ExitTimeOut"] >= 10          # graceful-shutdown window
    assert "__REPO__" not in rendered and "__LOGDIR__" not in rendered
    # the program is our wrapper script (which loads .env + exec python)
    assert any("run.sh" in a for a in d["ProgramArguments"])


def test_macos_scripts_exist():
    for name in ("run.sh", "install.sh", "uninstall.sh", "ctl.sh"):
        assert (MACOS / name).exists(), f"missing deploy/macos/{name}"


def test_plist_carries_no_secrets():
    # Secrets must come from .env at runtime, never be baked into the plist.
    text = (MACOS / "com.finance.signal.plist.template").read_text().lower()
    assert "token" not in text
    assert "webhook" not in text


# --------------------------------------------------------------------------- #
# preflight diagnostic
# --------------------------------------------------------------------------- #
def _preflight():
    import importlib.util
    spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_preflight_offline_checks_pass():
    pf = _preflight()
    assert pf.check_python() is True
    assert pf.check_imports() is True
    assert pf.check_settings() is True
    # discord check is non-blocking and must not raise regardless of config
    assert pf.check_discord() in (True, False)


def test_preflight_no_network_exits_zero(monkeypatch):
    pf = _preflight()
    monkeypatch.setattr(sys, "argv", ["preflight", "--no-network"])
    assert pf.main() == 0


def test_preflight_handles_yahoo_unreachable_gracefully(monkeypatch):
    """A Yahoo outage must be reported as non-fatal (sample fallback exists),
    never raise."""
    pf = _preflight()
    import signal_engine.live.yahoo_adapter as ya

    def boom(ticker, timeout):
        raise OSError("network down")

    monkeypatch.setattr(ya, "_urllib_transport", boom)
    # check_yahoo returns False but does not raise
    assert pf.check_yahoo() is False
