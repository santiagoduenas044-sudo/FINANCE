# Phase 13 — 24/7 Bot Runtime + Remote Dashboard

Goal: make the bot run **continuously and unattended** (Mac closed, Claude Code
shut) and prepare the architecture to reach the dashboard **remotely** from a
phone — without weakening any earlier guarantee.

```
Yahoo delayed ES/NQ → BotService → Analysis → Setup → Signal → Risk → Discord + Dashboard
```

Enforced throughout (unchanged): no broker, no order execution, no
auto-trading, no TradingView, no AI in the signal path; no-lookahead /
closed-bars-only; CME `verified: false` still blocks dollar sizing. The
protected engines — **Data, Market Analysis, Setup, Signal, Risk** — were not
modified; Phase 13 only *orchestrates and serves* them.

## 0. Phase 12 verification (done first)

- **Real Yahoo delayed data:** `bootstrap.build_live_feed` builds a
  `YahooDelayedAdapter` (`ES=F`/`NQ=F`) first and only falls back to the bundled
  ES sample if `connect()` fails. In this sandbox Yahoo egress is blocked, so it
  falls back — an environmental limit, not a code issue; on a normal network it
  uses real delayed data.
- **DELAYED reported honestly:** `YahooDelayedAdapter.is_delayed = True` →
  `LiveFeed.health()` reports `DELAYED`; the simulated fallback reports its own
  state and is now labelled **SIMULATED** everywhere in the UI.
- **Candles update / chart == bot data:** the chart reads the *same*
  `LiveFeed.get_tf_bars()` buffer and the *same* `MarketAnalyzer` the trading
  cycle uses (`signal_engine/api/chart.py`). No separate/mock data path.
- **No mock data in production:** the runner uses Yahoo→sample-fallback only;
  the earlier demo server was scratchpad-only and is not in the repo.
- Full suite green before changes.

## 1. 24/7 runtime — `signal_engine/runtime/service.py`

`BotService` wraps a `BotRuntime` and drives it on a fixed interval:

- **Automatic polling** every `cycle_seconds` (default 5s).
- **Duplicate-candle prevention:** the pipeline runs at most **once per closed
  execution candle per symbol**. The service polls once, checks whether the last
  closed 5-minute bar advanced, and skips (`duplicates_skipped++`) when it
  hasn't — no more re-processing the same bar every few seconds.
- **Automatic recovery / reconnection:** a tick never crashes the loop. Feed
  reconnection + backoff is handled by `LiveFeed` (Phase 10); per-cycle engine
  errors are caught by `BotRuntime`. Through an outage the bot stays `ONLINE`
  and the feed reports `DISCONNECTED`; when the source returns, data resumes on
  the next tick.
- **Heartbeat** even when paused/offline (dashboard liveness).
- **Clear states:** `BotStatus` (ONLINE / PAUSED / OFFLINE / ERROR) plus a
  computed **connection** state (`CONNECTED` / `DEGRADED` / `DISCONNECTED` /
  `CONNECTING` / `NONE`) — distinct from bot status, since the bot stays ONLINE
  while a feed reconnects. Exposed via `BotRuntime.connection_state()` and
  `/api/status.connection`.
- **Graceful startup/shutdown:** `run_forever()` installs SIGINT/SIGTERM
  handlers so `systemctl stop` / `docker stop` / Ctrl-C stop the loop cleanly
  (and stop the runtime if it was ONLINE). `start_background()` runs the same
  loop in a daemon thread for the dashboard.
- **Unattended logging:** timestamped lines to stdout (journald/Docker) + an
  optional rotating file (`bootstrap.configure_logging`).
- **Persistent history:** signals continue to be appended to
  `data_store/signal_log.jsonl`.

Two entry points share the exact same wiring (`signal_engine/runtime/
bootstrap.py`):

- `scripts/run_dashboard.py` — dashboard + bot loop (starts OFFLINE by default).
- `scripts/run_bot.py` — headless bot, no HTTP (starts ONLINE by default) — the
  smallest always-on process.

## 2. Dashboard polish

Kept the premium design; made it read like a real trading platform:

- **Delayed vs simulated** is now unmistakable: a `SIMULATED` amber chip + a
  full-width warning banner appear whenever the chart is on sample-fallback
  data, distinct from the blue `DELAYED` chip for real Yahoo quotes.
- **Connection indicator** in the top bar (CONNECTED / DEGRADED / DISCONNECTED).
- **Current price** shows the last price plus change and %; a right-axis price
  tag and a dashed last-price line track it on the chart.
- Clear ES/NQ switching, current session, last-update age.
- **Clickable signals** open the chart marker + full detail; **ACTIVE** trades
  pulse; win/loss/active counts per symbol.
- Nicer empty states; de-cluttered liquidity labels; smoother indicator lines.
- Responsive desktop + mobile (verified with a headless browser at 390px).

## 3. Remote configuration — `signal_engine/config/runtime_settings.py`

`ServerSettings` (env-driven, secret-free defaults) separates *how the process
is served* from *what the bot decides*:

- **Modes:** `local` (bind 127.0.0.1, no auth needed) vs `remote` (bind
  `BOT_HOST`, default 0.0.0.0). Remote mode **requires** an auth token —
  `validate()` refuses to start a public, unauthenticated server.
- **Host/port/symbols/cycle/auto-start** all configurable via env.
- **Auth placeholder:** a bearer token read from `$DASHBOARD_TOKEN` (never
  hard-coded, never returned in any response), accepted via
  `Authorization: Bearer …` or `?token=…` (phone-friendly). `/api/health` and
  the UI shell stay exempt. This is a placeholder — real deployments put TLS
  (VPN or reverse proxy) in front.
- **CORS:** applied only when `BOT_CORS_ORIGINS` lists explicit origins;
  wildcard `*` is rejected.

New endpoints: `GET /api/health` (unauthenticated liveness/readiness),
`GET /api/config` (secret-free), `GET /api/service` (loop diagnostics). Existing
endpoints and their tests are unchanged (new keys are additive).

## 4. Deployment

Simplest reliable options (pick one) — see `deploy/README.md`:

- **Docker:** `Dockerfile` + `docker-compose.yml` (`restart: unless-stopped`,
  container `HEALTHCHECK` on `/api/health`, `data_store` volume, non-root user,
  remote mode requiring `DASHBOARD_TOKEN`).
- **systemd:** `deploy/systemd/signal-dashboard.service` and `signal-bot.service`
  (`Restart=always`, graceful `SIGINT` stop, boot-enable, light hardening).

`.env.example` documents every variable. `.dockerignore` excludes the unrelated
legacy PWA/iOS assets.

## 5. Testing

`tests/test_phase13_service.py` (10) — continuous polling, duplicate-candle
prevention, feed outage + recovery, tick-never-raises, paused-doesn't-trade,
state/connection transitions, persistence, graceful shutdown, background loop.

`tests/test_phase13_remote.py` (14) — settings defaults/parsing, remote-requires-
token, CORS wildcard rejected, token never leaked, health open + secret-free,
auth gate (401/200/query-token/UI-exempt), CORS header present/absent,
config + service diagnostics.

All prior tests kept. Full suite: **353 passing** (329 from Phase 12 + 24 new).
UI re-verified end-to-end with a headless browser (desktop + mobile, zero
console errors).

## Run it

```bash
# Local dashboard (bot loop included; starts OFFLINE — press Start)
PYTHONPATH=. python scripts/run_dashboard.py         # http://127.0.0.1:8000/

# Headless 24/7 bot (starts ONLINE, no UI)
PYTHONPATH=. python scripts/run_bot.py

# Remote (VPS) — see deploy/README.md
BOT_MODE=remote DASHBOARD_TOKEN=$(openssl rand -hex 24) \
  PYTHONPATH=. python scripts/run_dashboard.py
```

## Answers to the four questions

1. **Running independently of Claude:** the whole pipeline —
   `run_dashboard.py` or `run_bot.py` drives `BotService`, which polls Yahoo
   delayed ES/NQ, runs Analysis→Setup→Signal→Risk, and publishes to Discord +
   the signal log — all with no IDE/Claude attached. Under Docker/systemd it
   restarts itself on crash and on reboot.
2. **Still requires your Mac:** nothing, once it's on a server. If you run it on
   the Mac itself, it lives only while that process/machine is up. Real Yahoo
   data also requires normal outbound internet (this sandbox lacks it, hence the
   sample fallback).
3. **To run 24/7 on a VPS:** a small Linux box with Python 3.11 (or Docker),
   outbound HTTPS to Yahoo, and either `docker compose up -d` or the systemd
   unit; set `BOT_MODE=remote` + `DASHBOARD_TOKEN` (+ optional
   `DISCORD_WEBHOOK_URL`). `deploy/README.md` has copy-paste steps.
4. **iPhone access:** it's a responsive web app. Reach it over a VPN
   (Tailscale/WireGuard — recommended) or an HTTPS reverse proxy /
   Cloudflare Tunnel, then open `https://<host>/?token=<DASHBOARD_TOKEN>` (the
   token is remembered after the first visit). Do not expose it unauthenticated
   over plain HTTP.
```
