"""ORBIT: a separate paper bot that trades the APERTURA strategy on NQ.

It is its own process (its own launchd service on the Mac), with its own
Discord channel and its own data folder. It shares nothing with GHOST but
the repository: GHOST's config, trades, limits and messages are untouched.

Every `POLL_SECONDS` it reads the delayed Yahoo 1-minute NQ bars (the same
source GHOST uses) and hands them to the APERTURA rules
(subbots/opening_range.py), which decide, notify and keep their own record.

    .venv/bin/python -m signal_engine.subbots.orbit          # what launchd runs
    .venv/bin/python -m signal_engine.subbots.orbit --probar # replay the last days
    .venv/bin/python -m signal_engine.subbots.orbit --probar --discord   # ...and post them
    .venv/bin/python -m signal_engine.subbots.orbit --probar --desde 2026-01-01  # Databento history

Environment (.env):
    DISCORD_WEBHOOK_ORBIT   webhook of ORBIT's own channel (without it ORBIT
                            only writes to its log; it never uses GHOST's)
    ORBIT_NAME              name shown in the messages (default ORBIT)
    ORBIT_POSITION          paper position (default "1 NQ"; "1 MNQ", "2 MNQ"...)
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
from .opening_range import OpeningRangeBot, OrbSettings

ROOT = Path(__file__).resolve().parents[2]
STORE_DIR = ROOT / "data_store" / "orbit"
WEBHOOK_ENV = "DISCORD_WEBHOOK_ORBIT"
POLL_SECONDS = 30

log = logging.getLogger("orbit")


DEFAULT_POSITION = "1 NQ"     # was 1 MNQ until 2026-09-29


def bot_name() -> str:
    return os.environ.get("ORBIT_NAME", "").strip() or "ORBIT"


def settings() -> OrbSettings:
    """APERTURA's rules for the position in $ORBIT_POSITION (default 1 NQ)."""
    pos = os.environ.get("ORBIT_POSITION", "").strip() or DEFAULT_POSITION
    try:
        return OrbSettings.for_position(pos)
    except (ValueError, KeyError):
        log.warning("ORBIT_POSITION=%r no se entiende; uso %s", pos, DEFAULT_POSITION)
        return OrbSettings.for_position(DEFAULT_POSITION)


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
    return OpeningRangeBot(store_dir, settings=settings(), publish=publish or make_publisher(),
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


def databento_bars(start: str, end: str | None = None,
                   hours: tuple[str, str] | None = ("09:00", "16:59")) -> pd.DataFrame:
    """NQ 1-minute history from the Databento file the lab uses
    (market_data/NQ/DATABENTO_1m.csv, open-labeled, naive ET), turned into
    what ORBIT sees live: CLOSE-labeled ET bars. Only 09:00-17:00 is kept by
    default — ORBIT does nothing outside it — so years replay in minutes
    (`hours=None` keeps the whole day)."""
    csv = ROOT / "market_data" / "NQ" / "DATABENTO_1m.csv"
    pq = csv.with_suffix(".parquet")
    if pq.exists():
        df = pd.read_parquet(pq)
    elif csv.exists():
        print("  (primera vez: leyendo el CSV grande, ~1 min)", flush=True)
        df = pd.read_csv(csv, index_col="datetime", parse_dates=True)
    else:
        raise SystemExit(f"No encuentro {csv.relative_to(ROOT)}")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.loc[start:end] if end else df.loc[start:]
    if hours is not None:
        df = df.between_time(*hours)
    df = df[["open", "high", "low", "close"]].astype(float)
    df.index = (df.index + pd.Timedelta(minutes=1)).tz_localize("America/New_York")
    return df


def replay(bars: pd.DataFrame, publish, store_dir: Path, window: int = 600,
           charts: bool = True) -> OpeningRangeBot:
    """Feed past bars one minute at a time to a fresh ORBIT in `store_dir`,
    exactly as the live loop would have seen them."""
    from ..output.trade_chart import render_trade_png
    bot = OpeningRangeBot(store_dir, settings=settings(), publish=publish, name=bot_name(),
                          chart=render_trade_png if charts else None)
    for i in range(1, len(bars) + 1):
        bot.on_bars(bars.iloc[max(0, i - window):i])
    return bot


def monthly(closed: list[dict]) -> str:
    """One line per month: trades, wins and dollars (ORBIT's position)."""
    months: dict[str, list[float]] = {}
    for t in closed:
        months.setdefault(t["day"][:7], []).append(t["usd"])
    lines = []
    for m, usd in sorted(months.items()):
        wins = sum(1 for u in usd if u > 0)
        lines.append(f"  {m}: {len(usd):>3} trades · gana {wins / len(usd):>4.0%} · "
                     f"{'+' if sum(usd) >= 0 else '−'}${abs(sum(usd)):,.0f}")
    return "\n".join(lines)


def test_run(to_discord: bool, desde: str | None = None, hasta: str | None = None,
             detalle: bool = False) -> int:
    """`--probar`: replay past bars through ORBIT (in a temporary folder, so
    the real record is untouched) and show what it would have said and done.
    Without --desde: Yahoo's last ~5 days. With --desde: the Databento
    history (months or years); long periods show the monthly summary only."""
    import tempfile
    if desde:
        print(f"Leyendo el historial de Databento desde {desde}...", flush=True)
        bars = databento_bars(desde, hasta)
    else:
        print("Bajando las velas de 1 min de NQ de los últimos días (Yahoo)...", flush=True)
        bars = yahoo_bars()
    if bars.empty:
        print("No hay velas en ese rango.")
        return 1
    days = bars.index.normalize().nunique()
    verbose = detalle or days <= 10
    print(f"{len(bars):,} velas: {bars.index[0]:%d-%b-%Y %H:%M} → {bars.index[-1]:%d-%b-%Y %H:%M}"
          f" ({days} días)\n", flush=True)
    if to_discord and days > 10:
        print("(con más de 10 días no mando nada a Discord, serían demasiados mensajes)\n")
        to_discord = False
    real = make_publisher() if to_discord else None

    def show(*, title, text, color=0x3498DB, key="", image=None):
        if verbose:
            print(f"── {title}\n   " + text.replace("**", "").replace("\n", "\n   ") + "\n",
                  flush=True)
        if real is not None:
            real(title=f"🧪 PRUEBA · {title}", text=text, color=color, key=f"test:{key}",
                 image=image)
    with tempfile.TemporaryDirectory() as tmp:
        bot = replay(bars, show, Path(tmp), charts=to_discord)
        print("════ Resultado de la prueba ════")
        print(bot.record_text().replace("**", ""))
        if days > 10 and bot.closed:
            print(f"\nPor mes ({bot.s.position}, con costos):")
            print(monthly(bot.closed))
    if to_discord and not os.environ.get(WEBHOOK_ENV, "").strip():
        print(f"\n(no se mandó nada a Discord: falta {WEBHOOK_ENV} en .env)")
    return 0


def _arg(name: str) -> str | None:
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    if "--probar" in sys.argv:
        logging.getLogger().setLevel(logging.WARNING)
        return test_run(to_discord="--discord" in sys.argv, desde=_arg("--desde"),
                        hasta=_arg("--hasta"), detalle="--detalle" in sys.argv)
    stop = {"now": False}

    def _quit(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, _quit)
    signal.signal(signal.SIGINT, _quit)

    publish = make_publisher()
    bot = build_bot(publish)
    name = bot.name
    log.info("%s en línea (estrategia APERTURA, %s en papel, datos en %s)", name,
             bot.s.position, STORE_DIR)
    try:
        publish(title=f"🛰️ {name} en línea",
                text="Estrategia **APERTURA**: caja de 9:30 a 9:45, entra cuando una vela de "
                     "5 min cierra fuera, stop del otro lado, objetivo 1.5× el stop, BE+2 al 70 %, "
                     f"cierre 16:55. {bot.s.position}, en papel. Aviso a las 9:45 con la caja "
                     "del día.",
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
