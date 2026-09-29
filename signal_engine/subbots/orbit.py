"""ORBIT: a separate paper bot that trades the APERTURA strategy on NQ.

It is its own process (its own launchd service on the Mac), with its own
Discord channel and its own data folder. It shares nothing with GHOST but
the repository: GHOST's config, trades, limits and messages are untouched.

Every `POLL_SECONDS` it reads the delayed Yahoo 1-minute NQ bars (the same
source GHOST uses) and hands them to the APERTURA rules
(subbots/opening_range.py), which decide, notify and keep their own record.

    .venv/bin/python -m signal_engine.subbots.orbit          # what launchd runs

Environment (.env):
    DISCORD_WEBHOOK_ORBIT   webhook of ORBIT's own channel (without it ORBIT
                            only writes to its log; it never uses GHOST's)
    ORBIT_NAME              name shown in the messages (default ORBIT)
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

import pandas as pd

from ..output.discord import DiscordOutputAdapter
from ..output.dispatcher import OutputDispatcher
from .opening_range import OpeningRangeBot

ROOT = Path(__file__).resolve().parents[2]
STORE_DIR = ROOT / "data_store" / "orbit"
WEBHOOK_ENV = "DISCORD_WEBHOOK_ORBIT"
POLL_SECONDS = 30

log = logging.getLogger("orbit")


def bot_name() -> str:
    return os.environ.get("ORBIT_NAME", "").strip() or "ORBIT"


def make_publisher():
    """ORBIT's own Discord channel, or just the log when no webhook is set."""
    if not os.environ.get(WEBHOOK_ENV, "").strip():
        log.warning("%s no está en .env: ORBIT solo escribe en su log.", WEBHOOK_ENV)

        def to_log(*, title, text, **_kw):
            log.info("%s | %s", title, text.replace("\n", " · "))
        return to_log
    from ..config.schema import DiscordOutputConfig
    cfg = DiscordOutputConfig(enabled=True, webhook_env=WEBHOOK_ENV)
    return OutputDispatcher([DiscordOutputAdapter(cfg)]).publish_note


def yahoo_bars() -> pd.DataFrame:
    """The last days of NQ 1-minute bars, CLOSE-labeled, in ET."""
    from ..live.yahoo_adapter import YahooDelayedAdapter
    bars = YahooDelayedAdapter()._fetch("NQ")
    return pd.DataFrame(
        {"open": [b.open for b in bars], "high": [b.high for b in bars],
         "low": [b.low for b in bars], "close": [b.close for b in bars]},
        index=pd.DatetimeIndex([b.timestamp for b in bars]))


def build_bot(publish=None, store_dir: Path = STORE_DIR) -> OpeningRangeBot:
    from ..output.trade_chart import render_trade_png
    return OpeningRangeBot(store_dir, publish=publish or make_publisher(),
                           chart=render_trade_png, name=bot_name())


def run_once(bot: OpeningRangeBot, fetch=yahoo_bars) -> bool:
    """One poll. False when the data could not be read (retried next time)."""
    try:
        bars = fetch()
    except Exception as exc:  # noqa: BLE001 - a bad poll must never stop the bot
        log.warning("sin datos de Yahoo (%s)", type(exc).__name__)
        return False
    bot.on_bars(bars)
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    stop = {"now": False}

    def _quit(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, _quit)
    signal.signal(signal.SIGINT, _quit)

    publish = make_publisher()
    bot = build_bot(publish)
    name = bot.name
    log.info("%s en línea (estrategia APERTURA, 1 MNQ en papel, datos en %s)", name, STORE_DIR)
    try:
        publish(title=f"🛰️ {name} en línea",
                text="Estrategia **APERTURA**: caja de 9:30 a 9:45, entra cuando una vela de "
                     "5 min cierra fuera, stop del otro lado, objetivo 1.5× el stop, BE+2 al 70 %, "
                     "cierre 16:55. 1 MNQ, en papel. Aviso a las 9:45 con la caja del día.",
                color=0x3498DB, key=f"online:{int(time.time())}")
    except Exception:  # noqa: BLE001
        pass
    while not stop["now"]:
        run_once(bot)
        for _ in range(POLL_SECONDS):
            if stop["now"]:
                break
            time.sleep(1)
    log.info("%s detenido.", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
