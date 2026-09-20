# FINANCE — Futures Paper-Trading Bot

Intraday signal detection and paper-trading system for ES and NQ futures (CME).
Uses Yahoo Finance 15-minute-delayed data. No broker connection. No real orders.

## Architecture

```
Data (Yahoo Finance delayed)
  → Market Analysis (regime, MTF, structure, liquidity, key levels)
  → Setup Engine   (ORB, VWAP reclaim, liquidity sweep, …)
  → Signal Engine  (confluence, dedup)
  → GHOST Brain    (authority: APPROVED | NO_TRADE)
  → Risk Engine    (position sizing, daily limits)
  → PaperTrade     (lifecycle: PENDING→ACTIVE→TP/STOP/CLOSED)
  → Dashboard      (FastAPI + canvas chart)
  → Discord        (webhook, APPROVED only)
```

## Instruments

| Symbol | Full contract | Micro |
|--------|--------------|-------|
| ES     | E-mini S&P 500 | MES |
| NQ     | E-mini Nasdaq-100 | MNQ |

## Quick Start (macOS)

```bash
# 1. Clone and install
git clone https://github.com/santiagoduenas044-sudo/finance.git ~/FINANCE
cd ~/FINANCE
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env — set BOT_DISCORD_ENABLED=true + DISCORD_WEBHOOK_URL to enable Discord

# 3. Run
python scripts/run_dashboard.py        # dashboard only
python scripts/run_bot.py              # bot + dashboard

# 4. 24/7 macOS service (launchd)
bash deploy/macos/install.sh           # installs launchd agent
bash deploy/macos/ctl.sh start
bash deploy/macos/ctl.sh status
bash deploy/macos/ctl.sh discord       # Discord activation diagnostic
```

## Dashboard

`http://localhost:8000` — **GHOST OPS**, a five-view signal-intelligence terminal:

| Vista | Qué muestra |
|---|---|
| **Tape** | Velas + VWAP/EMA, estructura (BOS/CHoCH, FVG, liquidez), zonas de la operación abierta, sesgo multi-timeframe y escalera de niveles clave |
| **Cerebro** | La cascada de confluencia: cada factor que suma o resta puntos, con su aportación exacta y el umbral marcado |
| **Embudo** | Conversión candidato → señal → aprobada, con la pérdida de cada etapa y su motivo |
| **Trades** | Curva de capital en R, distribución de resultados, operaciones abiertas y cerradas |
| **Auditoría** | Registro completo de decisiones GHOST + salud del motor |

Solo lectura salvo start/pause/stop. Set `DASHBOARD_TOKEN` in `.env` for token-auth
(required for remote access).

## Auditoría del motor de señales

`docs/SIGNAL_ENGINE_AUDIT.md` — qué usa el bot para buscar señales (spoiler: **no** cruces
de EMAs), y nueve hallazgos verificados empíricamente sobre dónde se pierden las señales.

## GHOST Authority

GHOST is the single decision authority. Only GHOST-APPROVED signals:
- open a PaperTrade
- trigger a Discord alert

NO_TRADE decisions never reach Discord or PaperTrade.

## Testing

```bash
pytest tests/ -x -q
```

## Deployment

See `deploy/macos/` for launchd setup, `deploy/vps/` placeholders for VPS.

## Security

- No broker credentials, no real money.
- `DISCORD_WEBHOOK_URL` lives in `.env` only — never in logs, API responses, or git.
- Dashboard token auth for remote access.
- All CME dollar-sizing is disabled pending official spec verification.
