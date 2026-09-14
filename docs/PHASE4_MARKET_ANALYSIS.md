# PHASE4_MARKET_ANALYSIS.md — Capa de Market Analysis

> Estado de la Fase 4. **Solo herramientas de análisis de mercado**, confiables y
> no-lookahead, que después consumirá el Setup Engine.
> **No** implementa: LONG/SHORT, estrategia, Setup Engine, Risk Engine, sizing,
> auto-trading, TradingView ni IA/LLM.
> Base: [`ARCHITECTURE.md`](./ARCHITECTURE.md) · [`ARCHITECTURE_RESEARCH.md`](../ARCHITECTURE_RESEARCH.md).

## 1) Arquitectura

```
AnalysisContext (as-of N, de Fase 3)
        │
        ▼
  MarketAnalyzer  ──────────────►  MarketAnalysis
        │                          (por timeframe: htf/structure/execution)
        ├── indicators.py  (VWAP, EMA, RSI, ATR, rel-volume)  [causales]
        └── MarketStructureProvider  (interfaz agnóstica de librería)
                 ├── NativeStructureProvider   (propio)
                 └── SmcStructureProvider       (wrapper de smart-money-concepts)
                          └── smart-money-concepts   ← 3rd party, NUNCA expuesto
```

**Decoupling (requisito clave):** los consumidores dependen de nuestros
**modelos de dominio** (`SwingPoint`, `StructureEvent`, `FairValueGap`,
`LiquidityLevel`, `OrderBlock`) y de la interfaz `MarketStructureProvider`.
`smart-money-concepts` está **envuelto** en `SmcStructureProvider` y su salida
posicional se traduce a nuestros modelos indexados por timestamp. Se puede
sustituir la librería (o usar el backend nativo) **sin tocar** Setup/Signal
Engine ni el Backtester. Factory: `get_structure_provider("smc"|"native")`.

## 2) Archivos creados

- `signal_engine/features/indicators.py`
- `signal_engine/features/analyzer.py` (fachada `MarketAnalyzer`)
- `signal_engine/features/market_structure/`: `models.py`, `base.py`,
  `_common.py` (labeling HH/HL/LH/LL), `native.py`, `smc_adapter.py`, `__init__.py`
- `scripts/demo_market_analysis.py`
- tests: `test_indicators.py`, `test_market_structure_native.py`,
  `test_market_structure_smc.py`, `test_backend_swappable.py`
- `conftest.py` (builders `make_bars_from_ohlc/closes`)

## 3) Componentes implementados

| Componente | Backend nativo | Backend smc |
|---|---|---|
| VWAP (anclado por sesión) | ✅ (indicators) | (indep. de backend) |
| EMA / RSI (Wilder) / ATR (Wilder) | ✅ | (indep.) |
| Volumen / relative volume | ✅ | (indep.) |
| Swings + HH/HL/LH/LL | ✅ | ✅ (swing_highs_lows) |
| BOS / CHoCH | ✅ | ✅ (bos_choch) |
| Fair Value Gaps | ✅ | ✅ (fvg) |
| Liquidity levels + sweeps | ✅ | ✅ (liquidity) |
| Order blocks | ✅ (proxy estructural) | ✅ (ob) |

## 4) Cómo se evita look-ahead en cada componente crítico

- **Contrato as-of:** todo componente recibe `bars` ya recortadas a `<= N` por el
  `AnalysisContext`. Ninguna función accede a barras fuera del frame.
- **Indicadores:** **causales** por construcción (EMA/RSI/ATR con
  `ewm(adjust=False)` y `shift(1)`; VWAP = cumulativo por sesión; rel-vol =
  rolling que termina en la barra actual). El valor en `i` sólo usa `<= i`.
- **Swings (nativo):** un swing en `i` requiere `n_right` barras a la derecha
  dentro del frame; `confirmed_at = ts[i+n_right]`. Los swings sin confirmar
  (cola) no se emiten.
- **Swings (smc):** smc marca swings **provisionales** en las últimas
  `swing_length` barras (el extremo en curso). El adapter **descarta la cola de
  `swing_length` barras** y sólo emite swings confirmados.
- **BOS/CHoCH:** el evento se fecha en la **barra de ruptura**; sólo se emite si
  la ruptura ya ocurrió dentro del frame (nativo: `close` cruza un swing
  confirmado; smc: `BrokenIndex` presente).
- **FVG:** patrón de 3 velas confirmado en la 3ª vela; se descartan gaps cuya
  formación no está completa. La mitigación se busca sólo dentro del frame (si
  aún no ocurrió por `N`, `mitigated_at = None`).
- **Liquidity / Order blocks:** basados en swings confirmados; sweeps/mitigación
  sólo dentro del frame.

**Tests de no-lookahead:** para cada detección se verifica *truncation
stability* — todo evento confirmado con datos `[:N]` reaparece idéntico con
`[:M]` (M>N). Campos hacia adelante (`mitigated_at`, `swept_at`) se excluyen de
la identidad y se verifican **monótonos** (una vez ocurridos, no se deshacen).
Cubre swings, BOS/CHoCH, FVG, liquidity (nativo y smc).

## 5) Tests y resultados

`python -m pytest -q` → **77 passed** (48 de Fase 3 + 29 de Fase 4).
Incluye: correctitud de indicadores (EMA manual, límites RSI, ATR>0, reset de
VWAP por sesión, rel-vol), detección de swings/labels, BOS→CHoCH, FVG +
mitigación, liquidity + sweep, order blocks, no-lookahead por componente
(nativo y smc), y sustituibilidad de backend (`MarketAnalyzer` con ambos).

## 6) Reutilización de smart-money-concepts vs propio

- **Reutilizado de smc** (vía `SmcStructureProvider`): `swing_highs_lows`,
  `bos_choch`, `fvg`, `liquidity`, `ob`. Aporta detección madura sin
  reimplementarla.
- **Propio (nativo):** todos los **indicadores** (smc no los cubre; VWAP
  necesita anclaje por sesión propio), el **etiquetado HH/HL/LH/LL**, y un
  **backend nativo completo** de estructura (swings, BOS/CHoCH, FVG, liquidity,
  OB) que sirve como implementación de producción y como **oráculo de
  referencia** en tests.
- **Salvaguarda añadida sobre smc:** filtrado de detecciones provisionales de
  cola (no-lookahead), que smc no garantiza por sí mismo.

## 7) Limitaciones / trade-offs

- **smc default, nativo alternativo:** honra la reutilización pedida; el nativo
  garantiza control total de no-lookahead y no depende de terceros. Ambos
  testeados. Sus resultados **difieren** (algoritmos/params distintos): es
  esperado; el Setup Engine elegirá backend por config.
- **Relative volume** es rolling-baseline, no *same-time-of-day* RVOL (más
  representativo intradía). Pendiente de mejora en una fase futura.
- **Order blocks nativos** usan un proxy estructural simple (última vela opuesta
  antes de la ruptura); smc ofrece una versión más rica (volumen/%).
- **Latencia de confirmación:** por diseño, swings/estructura aparecen con
  retraso (necesitan barras de confirmación). Es correcto (no-lookahead), pero
  el Setup Engine debe asumir esa latencia.
- **Parámetros** (`n_left/n_right`, `swing_length`, tolerancias) afectan
  sensibilidad; se dejaron configurables, sin optimizar (eso es fase posterior y
  sujeto a anti-overfitting).
- La verificación de **ContractSpec/CME sigue bloqueada** y no afecta a esta capa
  (Market Analysis no usa multiplicadores).
