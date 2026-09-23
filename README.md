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

## Escáner de acciones (`stock_scanner/`)

Módulo independiente del bot de futuros: analiza un universo de acciones con barras
**diarias** y las ordena por las mejores oportunidades de compra, con un plan concreto
(entrada, stop, objetivo y número de acciones) para cada candidata.

```bash
PYTHONPATH=. python scripts/scan_stocks.py                          # universo por defecto (~70 large caps USA)
PYTHONPATH=. python scripts/scan_stocks.py --tickers AAPL,NVDA,AMD,MSFT
PYTHONPATH=. python scripts/scan_stocks.py --capital 25000 --risk 0.5  # tamaño de posición a tu cuenta
PYTHONPATH=. python scripts/scan_stocks.py --backtest                 # ¿funciona el ranking? walk-forward
PYTHONPATH=. python scripts/scan_stocks.py --out reports/             # guarda scan.md + scan.json
PYTHONPATH=. python scripts/scan_stocks.py --provider synthetic       # demo offline (datos falsos)
```

**Score 0-100** = suma ponderada (pesos en `stock_scanner/config.yaml`):

| Factor | Peso | Qué mide |
|---|---|---|
| Momentum | 25% | Retorno 12-1 meses y 6 meses (percentil dentro del universo) |
| Fuerza relativa | 20% | Retorno 6m menos el del índice (SPY) |
| Tendencia | 20% | Precio > SMA50, > SMA200, SMA50 > SMA200, SMA50 subiendo |
| Cerca de máximos | 10% | Distancia al máximo de 52 semanas |
| Acumulación | 10% | Volumen en días alcistas vs bajistas (50 sesiones) |
| Timing | 10% | Cerca de la EMA20 y RSI 40-65 (no perseguir el precio) |
| Baja volatilidad | 5% | Volatilidad realizada 60d |

**Señal:** `COMPRAR` si score ≥ 70, precio sobre la SMA200 y RSI < 75 · `VIGILAR` si score ≥ 55
(p. ej. líderes sobrecompradas: esperar retroceso) · `EVITAR` el resto.
**Setup:** ruptura de máximos, retroceso en tendencia, momentum alcista, rebote sobrevendido,
sobreextendida o tendencia bajista. **Plan:** stop = entrada − 2×ATR14, objetivo a 2R, tamaño
para arriesgar el 1% del capital (máx. 20% del capital por posición).

Filtros previos: precio ≥ $5, volumen medio ≥ $20M/día, ≥ 210 sesiones de historial.
Datos: Yahoo Finance (gratis, sin API key, ajustados por splits/dividendos); también CSV
(`--provider csv --csv-dir carpeta/`). El backtest tiene sesgo de supervivencia (usa el
universo actual): úsalo para comparar configuraciones, no como promesa de rentabilidad.
Herramienta de análisis — no es asesoría financiera ni envía órdenes.

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
