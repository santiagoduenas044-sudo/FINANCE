# PHASE8_BACKTESTING.md — Backtesting Engine

> Fase 8. `Setup Engine → Signal Engine → (opcional) Risk Engine → resolución de
> trades → métricas`. Reutiliza los motores existentes tal cual — **cero
> reimplementación** de detección/validación.
> Base: [`PHASE6_SIGNAL_ENGINE.md`](./PHASE6_SIGNAL_ENGINE.md) ·
> [`PHASE7_RISK_ENGINE.md`](./PHASE7_RISK_ENGINE.md) · [`CME_VERIFICATION.md`](./CME_VERIFICATION.md).

## 1) Arquitectura

```
DataEngine.analysis_context(N) ──► SetupEngine.detect(ctx) ──► SetupCandidate[]
                                                                       │
                                          SignalEngine.evaluate_candidates(ctx, ·)
                                                                       │
                                                        Signal | NoTrade[]
                                                                       │
                              ┌────────────────────────────────────────┤
                              ▼                                        ▼
                  simulator.resolve_trade_outcome         (opcional) RiskEngine.evaluate
                  (solo precios del Signal: WIN/LOSS/       (gateado por require_verified(),
                   EXPIRED/OPEN)                             igual que Fase 7)
                              │                                        │
                              └──────────────► ClosedTrade ◄───────────┘
                                                     │
                                          metrics.compute_metrics()
                                                     │
                                            BacktestResult
```

`signal_engine/backtesting/` **no importa nada** de Discord/output/broker/
Streamlit/Ollama/IA. **Cero cambios** en `setups/`, `signals/`, `risk/`,
`output/`, `features/`, `data/`, `config/` — verificado con `git diff --stat`.

## 2) Archivos creados

- `signal_engine/backtesting/{__init__.py, models.py, simulator.py, dollars.py, metrics.py, engine.py}`
- `tests/test_backtesting.py` (26 tests)
- `scripts/demo_backtesting.py`
- `docs/PHASE8_BACKTESTING.md`

## 3) Dos capas de resultado, deliberadamente desacopladas

**Capa R-multiple (siempre disponible):** `resolve_trade_outcome(signal, bars)`
determina qué pasó tras una señal usando **solo** sus propios precios
(entry/stop/TP1) y las barras posteriores — **sin `ContractSpec`, sin
dólares, sin dependencia de CME**. Esto significa que el backtesting **funciona
hoy mismo**, con el registro real (`verified: false`).

**Capa en dólares (gateada, opcional):** solo se calcula si se provee un
`RiskEngine` respaldado por un registro **verificado**. Usa exactamente el
mismo `require_verified()` de la Fase 7 — **no se duplica ni se debilita**. Si
el registro real (no verificado) se usa, `RiskEngine.evaluate()` devuelve
`REJECTED/UNVERIFIED_CONTRACT_SPEC` para cada señal, y
`compute_dollar_result()` simplemente no se ejecuta (`dollar=None`). Los
tests de dólares usan un registro **hipotético en memoria** (mismo patrón que
Fases 6.5/7 — nunca toca `contracts.yaml`, nunca reclama verificación real).

## 4) Resolución de trades — reglas exactas

- Se camina barra a barra **estrictamente después** de `signal.timestamp`.
- **WIN**: primer toque de TP1 (`high >= tp1` en LONG, `low <= tp1` en SHORT). R = `signal.risk_reward[0]`.
- **LOSS**: primer toque del stop. R = `-1.0` (por definición: perder implica perder exactamente 1R).
- **Ambos tocados en la misma barra**: como los OHLC no revelan el orden intra-barra, se asume **conservadoramente que el stop se tocó primero** (LOSS). Limitación documentada, no oculta.
- **EXPIRED**: si `signal.expiration` está definido y pasa sin resolución.
- **OPEN**: si los datos históricos se agotan antes de resolución (y no hay `expiration`, o no se alcanzó).
- Sin `take_profits`, el trade solo puede ser LOSS u OPEN (nunca WIN).

## 5) Costos — solo en la capa de dólares

`CostModel(slippage_ticks, commission_per_contract_per_side)`. El slippage se
aplica **en contra** en la entrada y en la salida (peor precio de fill); la
comisión se cobra en ambos lados (entrada + salida) por contrato. La capa
R-multiple permanece **idealizada y sin costos** — limitación documentada
(depende de `tick_size`, un dato CME, así que igual queda gateada por
verificación si se intentara aplicar ahí).

## 6) Métricas

Siempre (R-multiple): `total_signals`, `no_trade_count`, `resolved_trades`,
`wins/losses/expired/open`, `win_rate`, `avg_win_r`, `avg_loss_r`,
`profit_factor`, `expectancy_r`, `net_r`, `max_drawdown_r`, `sharpe_r`/
`sortino_r` (por trade, **no anualizados** — no hay un intervalo de tiempo
fijo entre trades), `max_consecutive_wins/losses`, desgloses `by_instrument`,
`by_setup`, `by_direction`.

Solo si **todos** los trades resueltos tienen resultado en dólares:
`net_pnl_dollars`, `max_drawdown_dollars`, `avg_win_dollars`,
`avg_loss_dollars`, `total_commission_dollars`. Si no, quedan en `None` con
`dollar_metrics_reason` explicando por qué (nunca un `None` silencioso).

## 7) No-lookahead

- **Generación de señales**: cada barra de decisión N usa `AnalysisContext`
  as-of N (igual que Fases 5/6) — el bucle del backtester no añade ninguna
  fuga: test dedicado compara una corrida completa vs. una corrida truncada a
  mitad de camino y confirma que las señales generadas hasta el corte son
  **idénticas**.
- **Resolución de outcome SÍ mira barras futuras — y es correcto.** Esto no es
  una violación: resolver "qué pasó después de la señal" requiere,
  por definición, ver lo que pasó después. Se documenta explícitamente para
  no confundirlo con el no-lookahead de generación (test dedicado que separa
  ambos conceptos).

## 8) Decisiones y limitaciones (para tu revisión)

1. **Tie-break conservador** (stop antes que TP si ambos se tocan en la misma
   barra) — sesga las métricas levemente pesimistas; alternativa sería usar
   barras de menor timeframe para desambiguar (fuera de alcance).
2. **R de pérdida siempre exactamente -1.0** — no modela salidas parciales ni
   slippage en la capa R (ver §5). Es la convención estándar de R-multiple.
3. **Sharpe/Sortino por trade, no anualizados** — los trades no ocurren a
   intervalos fijos; anualizar requeriría asumir una frecuencia, lo cual sería
   inventar un dato. Documentado como aproximación.
4. **Sin Walk-Forward ni Monte Carlo** — mencionados en la investigación
   original (Fase 1) pero **fuera de alcance de esta fase** por instrucción
   general de "no optimizar todavía"; quedan para una fase futura de
   validación de estrategia.
5. **Sin ejecución/broker/dashboard** — el backtester es puro cálculo offline.
6. **Rendimiento**: `MarketAnalyzer` recalcula estructura desde cero en cada
   barra (limitación conocida desde la Fase 5, O(N²) en un run). Corridas de
   varios días tardan segundos-minutos; aceptable para esta fase, optimizable
   después con caché incremental.

## 9) Tests y resultados

`python -m pytest -q` → **242 passed** (216 previos de Fases 3–7 + 26 nuevos
de Fase 8). Sin red.

Cobertura: simulador (WIN/LOSS/EXPIRED/OPEN/empate-en-barra/SHORT/sin
TP/ignora barras `<=` timestamp), métricas (win rate, expectancy, profit
factor, drawdown, rachas, desgloses, vacío, profit factor infinito, dólares
en `None` sin resultado en dólares), pipeline de dólares (aprobado/rechazado/
no-resuelto/slippage/comisión), motor end-to-end (R-only con registro real no
verificado, dólares completos con registro hipotético, determinismo,
no-lookahead de generación vs. truncado, distinción explícita entre
no-lookahead de generación y resolución legítima con barras futuras).

## 10) Demo (`scripts/demo_backtesting.py`)

Sobre los datos de muestra: **165 señales, 136 resueltas** (65 wins / 71
losses / 29 expired), win_rate 47.8%, expectancy -0.04R — resultados
idénticos en ambas corridas (R-only con registro real vs. dólares con
registro hipotético), confirmando que la capa de dólares nunca altera qué
señales se generan o cómo se resuelven, solo les añade una segunda capa de
información monetaria cuando está disponible.
