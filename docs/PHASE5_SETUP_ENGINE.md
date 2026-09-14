# PHASE5_SETUP_ENGINE.md — Setup Engine

> Estado de la Fase 5. Tres setups **independientes, deterministas y
> backtesteables**: Liquidity Sweep, VWAP Reclaim, ORB.
> **No** implementa: Signal Engine, scoring, "AI confidence", optimización de
> parámetros, selección del "mejor" setup, Risk Engine/sizing, ejecución,
> TradingView ni IA/LLM. **No** convierte candidatos en LONG/SHORT definitivo.
> Base: [`PHASE4_MARKET_ANALYSIS.md`](./PHASE4_MARKET_ANALYSIS.md) · [`ARCHITECTURE.md`](./ARCHITECTURE.md).

## 1) Arquitectura

```
AnalysisContext (as-of N)  ─►  MarketAnalyzer  ─►  MarketAnalysis
                                                       │
                    ┌──────────────────────────────────┤ (inyectado)
                    ▼                ▼                  ▼
        LiquiditySweepDetector  VwapReclaimDetector  OrbDetector   (independientes)
                    └──────────────┬───────────────────┘
                                   ▼
                          list[SetupCandidate]      ─►  (futuro) Signal Engine
```

`SetupEngine` construye solo los detectores **habilitados por config**, ejecuta
cada uno sobre el mismo `MarketAnalysis` as-of N, y agrega los `SetupCandidate`.
**No** rankea, puntúa ni decide LONG/SHORT.

## 2) Archivos creados/modificados

- `signal_engine/setups/`: `models.py` (SetupCandidate, SetupStatus, LevelRef,
  `risk_reward`, `update_status`), `base.py` (interfaz), `_helpers.py`,
  `liquidity_sweep.py`, `vwap_reclaim.py`, `orb.py`, `engine.py`, `__init__.py`
- `config/schema.py` + `config/default.yaml`: sección `setups` (toggles + params)
  y `analysis` (backend de estructura)
- `scripts/demo_setups.py`
- tests: `test_setup_models.py`, `test_setup_liquidity_sweep.py`,
  `test_setup_vwap_reclaim.py`, `test_setup_orb.py`, `test_setup_engine.py`
- **fix (regresión Fase 3):** `data/resample.py` — los bins vacíos de ETH tenían
  `volume=0` (no NaN), así que `dropna(how="all")` no los eliminaba, produciendo
  timestamps duplicados/filas NaN. Ahora se descartan por OHLC. Test de regresión
  añadido. (Los tests RTH-only no lo detectaban.)

## 3) Modelo de dominio `SetupCandidate`

`setup_name, instrument, direction (BULLISH/BEARISH — NO es LONG/SHORT), timestamp
(barra N de confirmación), timeframe, entry, stop_loss, take_profits[],
risk_reward[] (solo precio), reasons[], invalidation_conditions[],
invalidation_price, valid_until (expiración), market_context{}, referenced[]
(LevelRef), status (VALID/INVALID/EXPIRED)`.

`update_status(candidate, bars_posteriores)` gestiona el ciclo de vida
(determinista, para capas futuras): INVALID si se viola el nivel protector,
EXPIRED si pasa `valid_until` sin resolución, VALID en otro caso.

## 4) Cuándo se considera VÁLIDO cada setup (definición exacta)

**Liquidity Sweep** (sweep + reclaim de una sola barra, confirmado en N):
- Existe un **nivel de liquidez** detectado por Market Analysis y formado
  **estrictamente antes** de N (equal highs = buy-side/BULLISH; equal lows =
  sell-side/BEARISH).
- **Sweep sell-side** (nivel L de lows): `low[N] < L` y `close[N] > L` ⇒ bias
  **BULLISH**. **Sweep buy-side** (nivel H de highs): `high[N] > H` y
  `close[N] < H` ⇒ bias **BEARISH**.
- Entry = `close[N]`. Stop = extremo del sweep (low[N] o high[N]) ± buffer.
  Targets = liquidez opuesta (si existe) + múltiplos R. Invalidación = cierre más
  allá del extremo del sweep. Info requerida antes de declarar: el nivel
  pre-existente + el OHLC de la barra N (nada del futuro).

**VWAP Reclaim** (cruce de una barra, misma sesión, VWAP anclado por sesión):
- **BULLISH**: barra previa cierra `<=` VWAP y `close[N] >` VWAP, habiendo estado
  por debajo en la ventana `lookback`. **BEARISH**: simétrico.
- La barra previa debe ser de la **misma sesión/trading_date** que N (VWAP
  anclado). Entry = `close[N]`. Stop = swing estructural (o mínimo/máximo local)
  ± buffer. Targets = múltiplos R.

**ORB** (breakout tras opening range completo):
- OR = `[apertura_sesión, apertura + opening_range_minutes]` (RTH por defecto).
- **El breakout NUNCA se detecta antes de que el OR esté completo**: se exige
  `N > fin_OR`. OR high/low se calculan solo con barras del OR (todas `<= fin_OR
  <= N`).
- **BULLISH**: `close[N] > OR_high` y la barra previa cerró dentro del rango
  (cruce). **BEARISH**: `close[N] < OR_low` y previa dentro.
- Entry = `close[N]`. Stop = lado opuesto del OR. Targets = movimientos medidos
  del rango OR (`entry ± r*OR_range`).

En cualquier otro caso → **no se emite candidato** (equivalente a "no setup").

## 5) Cómo se evita look-ahead en cada punto crítico

- Todos los detectores reciben el `AnalysisContext` as-of N (barras recortadas a
  `<= N`) y solo leen la barra N y anteriores.
- **timestamp de entrada = N**, la barra de confirmación; la información necesaria
  (sweep, reclaim, breakout) está toda en N.
- **SL/TP** derivan de datos `<= N`: extremo del sweep en N, swing confirmado
  `< N`, límites del OR (ventana `<= fin_OR <= N`). Nunca de máximos/mínimos
  futuros.
- **ORB**: `N > fin_OR` garantiza que el OR está formado; OR high/low no cambian
  al añadir barras futuras (test dedicado).
- **Niveles referenciados**: `ref.timestamp <= N` (test).

**Tests de no-lookahead**: para cada setup, añadir barras futuras (y re-recortar a
N) no cambia el candidato; y end-to-end, re-detectar en cada N con datos truncados
a N da candidatos **idénticos** a los del dataset completo (0 discrepancias).

## 6) Tests y resultados

`python -m pytest -q` → **102 passed** (77 de Fases 3-4 + 24 de Fase 5 + 1 test de
regresión del resample). Los conteos "por archivo" abajo son funciones de test;
el total 102 incluye la expansión de tests parametrizados.

Específicos por setup:
- **Liquidity Sweep** (`test_setup_liquidity_sweep.py`, 5): detección bullish
  exacta (entry/stop), no-reclaim → sin candidato, nivel debe pre-existir N,
  sweep buy-side → bearish, no-lookahead.
- **VWAP Reclaim** (`test_setup_vwap_reclaim.py`, 5): reclaim bullish exacto,
  ya-por-encima → nada, pérdida bearish, **ancla de sesión** (no cruza sesión),
  no-lookahead.
- **ORB** (`test_setup_orb.py`, 5): breakout tras OR (valores exactos), **sin
  breakout antes de formar OR**, **sin breakout exactamente en fin_OR**, niveles
  OR estables con futuro, breakdown bearish.
- **Modelo** (`test_setup_models.py`, 4): `risk_reward`, transiciones
  INVALID/EXPIRED/VALID.
- **Engine** (`test_setup_engine.py`, 5): toggles individuales, **independencia**
  (Liquidity funciona con los otros dos desactivados), los tres disparan,
  **no-lookahead end-to-end**, timestamp de entrada = N y refs `<= N`.

## 7) Ejemplos de candidatos (demo real, `scripts/demo_setups.py`)

```
[vwap_reclaim] ES BEARISH @ 06-07 19:00  entry=4999.84 stop=5006.25 tp=[4993.43,4987.02] rr=[1.0,2.0]
  reasons: lost session VWAP (5000.04); prior above, N below
[liquidity_sweep] ES BEARISH @ 06-07 19:50 entry=5005.12 stop=5008.87 tp=[5001.37,5000.52,4997.62] rr=[1.0,1.23,2.0]
  reasons: swept buy-side liquidity 5006.07; high pierced, closed back below
[orb] ES BULLISH @ 06-08 10:05  entry=5013.84 stop=5006.46 tp=[5019.15,5024.46] rr=[0.72,1.44]
  reasons: OR [5006.46,5011.77] formed; closed above OR high
```

## 8) Supuestos y trade-offs

- **Confirmación de una sola barra** (sweep/reclaim/breakout ocurren y se
  confirman en la barra N). Es la definición más estricta y sin ambigüedad de
  look-ahead. Alternativa (multi-barra) queda para decisión (§9).
- **Entry = close[N]** (entrada al cierre de confirmación). No modela slippage ni
  next-bar-open (eso es del backtester/ejecución).
- **R:R solo con precios** (sin multiplicador/$): el Setup Engine **no** depende
  de ContractSpec verificado. El único uso de `tick_size` es un pequeño buffer de
  precio del stop (rejilla de precio, no cálculo monetario).
- **Volumen de candidatos**: con la tolerancia de liquidez por defecto el backend
  nativo genera **muchos** niveles/sweeps (p.ej. 623 en el sample). Es esperado
  en esta fase (correctitud, no filtrado): el Setup Engine no filtra ni rankea;
  eso vendrá con Signal Engine/scoring y el ajuste (no-optimizado aún) de
  parámetros.
- **Backend de estructura** configurable (native/smc); afecta swings/liquidez y
  por tanto a Liquidity Sweep. Default `native`.
- **Coste computacional**: `MarketAnalyzer` recomputa estructura sobre la vista
  as-of en cada barra (O(N²) en un scan). Correcto primero; optimizable después
  (caché incremental).

## 9) Ambigüedades que requieren tu decisión

1. **Definición de sweep**: ¿una sola barra (pierce + reclaim en N, actual) o
   multi-barra (pierce en una barra, reclaim dentro de K barras)?
2. **Modelo de entry**: ¿`close[N]` (actual), next-bar-open, o límite en el
   nivel? Impacta backtest/ejecución.
3. **Confirmación VWAP Reclaim**: ¿cruce de una barra (actual) o exigir K cierres
   consecutivos más allá de VWAP?
4. **ORB**: ¿`opening_range_minutes` = 30 (actual)? ¿solo RTH? ¿solo el primer
   breakout (actual) o permitir re-tests/segundo breakout?
5. **Lógica de targets**: hoy difiere por setup (Liquidity: liquidez opuesta + R;
   VWAP: R; ORB: medida del rango). ¿Unificar o mantener por setup?
6. **Convención de bias**: sweep sell-side→BULLISH, buy-side→BEARISH; reclaim
   arriba→BULLISH. ¿Confirmas? (se traduce a LONG/SHORT en Signal Engine).
7. **Tolerancia de liquidez / recencia**: ¿qué tolerancia de "equal highs/lows" y
   ventana de recencia quieres por defecto para reducir ruido? (sin optimizar).
8. **Buffer de stop**: hoy `stop_buffer_ticks * tick_size` (0.25). ¿Prefieres un
   buffer en ATR o en puntos puros para independizarlo por completo de
   ContractSpec?

> Nota: la verificación **CME/ContractSpec sigue bloqueada**; el Setup Engine no
> depende de valores monetarios verificados (solo `tick_size` como rejilla de
> precio para el buffer).
