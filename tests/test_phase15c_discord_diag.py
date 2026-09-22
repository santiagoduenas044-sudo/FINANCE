"""Phase 15c — Discord activation diagnostics.

The user hit `component_health["discord"].detail == "no Discord adapter attached
(disabled or not configured)"` after setting BOT_DISCORD_ENABLED=true + a
webhook in .env. That catch-all message hid the actual cause. These tests lock
in the improved reporting so a future regression is loud:

  * DISABLED detail names the *actual* reason: env flag false, YAML off + no
    env flag, or webhook missing.
  * BOT_DISCORD_ENABLED with case/whitespace variants activates correctly.
  * The env-flag override still leaves GHOST as the authority (no bypass).

No secrets are printed anywhere. The webhook is checked only for presence.
"""
import subprocess
from pathlib import Path

import pytest

from signal_engine.config.schema import EngineConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.output.dispatcher import OutputDispatcher, _env_bool
from signal_engine.runtime.engine import BotRuntime
from signal_engine.runtime.models import ComponentStatus
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine


ROOT = Path(__file__).resolve().parent.parent


def _runtime(monkeypatch=None, engine=None, registry=None,
             dispatcher=None) -> BotRuntime:
    return BotRuntime(engine, SetupEngine(engine.cfg, registry),
                      SignalEngine(engine.cfg.signals), registry,
                      output_dispatcher=dispatcher)


# --------------------------------------------------------------------------- #
# _env_bool robustness
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("val,expected", [
    ("true", True), ("True", True), ("TRUE", True), ("yes", True), ("1", True),
    ("on", True), (" true ", True), (" 1 ", True),
    ("false", False), ("False", False), ("no", False), ("0", False), ("off", False),
    ("", None),          # empty => unset
    ("nonsense", False), # anything else => not truthy
])
def test_env_bool_variants(monkeypatch, val, expected):
    if val == "":
        monkeypatch.delenv("BOT_DISCORD_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BOT_DISCORD_ENABLED", val)
    assert _env_bool("BOT_DISCORD_ENABLED") is expected


# --------------------------------------------------------------------------- #
# Health detail reports the ACTUAL cause instead of the catch-all
# --------------------------------------------------------------------------- #
def test_health_detail_env_flag_false(engine, registry, monkeypatch):
    """User explicitly set BOT_DISCORD_ENABLED=false — health should say so."""
    monkeypatch.setenv("BOT_DISCORD_ENABLED", "false")
    d = OutputDispatcher.from_config(engine.cfg)
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    output_dispatcher=d)
    h = rt.component_health["discord"]
    assert h.status is ComponentStatus.DISABLED
    assert "BOT_DISCORD_ENABLED" in h.detail
    assert "false" in h.detail.lower()


def test_health_detail_env_flag_unset_yaml_off(engine, registry, monkeypatch):
    monkeypatch.delenv("BOT_DISCORD_ENABLED", raising=False)
    engine.cfg.output.discord.enabled = False   # the state under test
    d = OutputDispatcher.from_config(engine.cfg)
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    output_dispatcher=d)
    h = rt.component_health["discord"]
    assert h.status is ComponentStatus.DISABLED
    assert "BOT_DISCORD_ENABLED" in h.detail
    assert "not set" in h.detail


def test_health_detail_webhook_missing(engine, registry, monkeypatch):
    """Env flag on but webhook not in env: adapter WOULD be attached (with a
    warning) — the user's originally-observed case is 'flag off' not this one,
    but we still expose it. The adapter is attached; the health check falls
    through to the second branch and reports webhook_not_configured / DEGRADED."""
    monkeypatch.setenv("BOT_DISCORD_ENABLED", "true")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    d = OutputDispatcher.from_config(engine.cfg)
    assert len(d.adapters) == 1                     # attached but unusable
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    output_dispatcher=d)
    h = rt.component_health["discord"]
    assert h.status is ComponentStatus.DEGRADED
    assert "webhook" in h.detail.lower()


def test_health_reports_ok_when_activated(engine, registry, monkeypatch):
    monkeypatch.setenv("BOT_DISCORD_ENABLED", "true")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example.invalid/x")
    d = OutputDispatcher.from_config(engine.cfg)
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    output_dispatcher=d)
    h = rt.component_health["discord"]
    assert h.status is ComponentStatus.OK, f"detail: {h.detail}"


# --------------------------------------------------------------------------- #
# run.sh .env parser robustness (regression test for the whole class of
# silent BOM / spaces-around-= / CRLF / quotes / export / bad-line bugs).
# --------------------------------------------------------------------------- #
RUN_SH = ROOT / "deploy" / "macos" / "run.sh"


def _run_parser(env_bytes: bytes, tmp_path: Path) -> str:
    """Extract the .env parser block from run.sh, run it against `env_bytes`,
    and echo the resulting BOT_DISCORD_ENABLED value. Returns stdout."""
    parser = ""
    inside = False
    for line in RUN_SH.read_text().splitlines():
        if line.startswith("# Load .env if present"):
            inside = True
        if inside:
            parser += line + "\n"
        if inside and line == "fi":
            break
    parser += 'echo "GOT=[${BOT_DISCORD_ENABLED-}]"\n'
    env = tmp_path / ".env"
    env.write_bytes(env_bytes)
    script = tmp_path / "p.sh"
    script.write_text(f'set -euo pipefail\nREPO_DIR="{tmp_path}"\n{parser}')
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=True)
    return r.stdout


def test_run_sh_env_parser_handles_bom(tmp_path):
    out = _run_parser(b"\xEF\xBB\xBFBOT_DISCORD_ENABLED=true\n", tmp_path)
    assert "GOT=[true]" in out


def test_run_sh_env_parser_handles_crlf(tmp_path):
    out = _run_parser(b"BOT_DISCORD_ENABLED=true\r\n", tmp_path)
    assert "GOT=[true]" in out


def test_run_sh_env_parser_handles_spaces_around_equals(tmp_path):
    out = _run_parser(b"BOT_DISCORD_ENABLED = true\n", tmp_path)
    assert "GOT=[true]" in out


def test_run_sh_env_parser_handles_quotes(tmp_path):
    out = _run_parser(b'BOT_DISCORD_ENABLED="true"\n', tmp_path)
    assert "GOT=[true]" in out


def test_run_sh_env_parser_handles_export_prefix(tmp_path):
    out = _run_parser(b"export BOT_DISCORD_ENABLED=true\n", tmp_path)
    assert "GOT=[true]" in out


def test_run_sh_env_parser_skips_malformed_line_but_continues(tmp_path):
    out = _run_parser(b"INVALID LINE\nBOT_DISCORD_ENABLED=true\n", tmp_path)
    assert "GOT=[true]" in out


def test_run_sh_env_parser_skips_comments_and_blank_lines(tmp_path):
    out = _run_parser(b"# comment\n\nBOT_DISCORD_ENABLED=true\n", tmp_path)
    assert "GOT=[true]" in out


# --------------------------------------------------------------------------- #
# GHOST authority is NOT bypassed by the env flag (guardrail regression)
# --------------------------------------------------------------------------- #
def test_env_flag_does_not_bypass_ghost_authority(engine, registry, monkeypatch):
    """Even when BOT_DISCORD_ENABLED=true, a Ghost-rejected signal must NOT
    reach Discord. Repeats the Phase-15b guarantee under the env-flag path."""
    import json
    from signal_engine.config.schema import GhostConfig
    from signal_engine.data.ingestion import normalize
    from signal_engine.data.providers.csv_provider import CsvProvider
    from signal_engine.ghost.brain import GhostBrain
    from signal_engine.live.feed import LiveFeed
    from signal_engine.live.simulated import SimulatedLiveAdapter
    from signal_engine.paper.tracker import PaperTradeTracker

    monkeypatch.setenv("BOT_DISCORD_ENABLED", "true")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example.invalid/x")
    sent = []
    def transport(url, data, timeout):
        p = json.loads(data)
        embed = p.get("embeds", [{}])[0]
        fields = {f["name"]: f["value"] for f in embed.get("fields", [])}
        if fields.get("Direction") in ("LONG", "SHORT"):
            sent.append(p)
        return 204
    dispatcher = OutputDispatcher.from_config(engine.cfg)
    assert len(dispatcher.adapters) == 1
    dispatcher.adapters[0]._transport = transport

    hist = normalize(CsvProvider(ROOT / "tests" / "data_samples")
                     .get_raw_bars("ES", "1m", contract="ESM26"), engine.cfg.data, "1m")
    feed = LiveFeed(SimulatedLiveAdapter(hist, symbol="ES", bars_per_poll=5),
                    "ES", engine.cfg)
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    live_feeds={"ES": feed},
                    ghost=GhostBrain(GhostConfig(min_confluence_score=99999.0)),
                    paper_tracker=PaperTradeTracker(),
                    output_dispatcher=dispatcher)
    rt.start()
    for _ in range(120):
        rt.run_cycle("ES")
    assert sent == [], "GHOST authority bypass would send rejected signals to Discord"
