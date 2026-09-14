# ARCHITECTURE_RESEARCH.md

> **Fase 1 — Investigación previa al desarrollo**
> Sistema de generación de señales para futuros de índices (NQ / ES; después MNQ / MES).
> **Este documento NO implementa la estrategia.** Es la investigación que precede al diseño e implementación.
>
> Fecha: 2026-09-14
> Estado del proyecto: el repositorio contiene actualmente una app iOS/PWA no relacionada. El signal engine se construirá como un subsistema nuevo e independiente dentro del repo.

---

## 0. Resumen ejecutivo (TL;DR)

| Pregunta | Respuesta corta |
|---|---|
| ¿Repo base único? | **No.** Ninguno sirve como base "clonar y renombrar". Ensamblaje best-of-breed. |
| ¿Qué se reutiliza? | `joshyattridge/smart-money-concepts` (MIT) como **librería de detección de estructura ICT/SMC**, envuelta detrás de nuestras interfaces. Indicadores clásicos con `pandas-ta`. Patrones (no código) del repo n8n para *killzones* y scoring de confluencia. |
| ¿Qué se construye desde cero? | El **Signal Engine**, el **Risk Engine**, el **modelo de datos de futuros (rollover/sesiones)**, el **backtester event-driven** y la **capa de output/adaptadores**. |
| Stack recomendado | **Python 3.11+**, pandas/numpy, pandas-ta, pydantic, FastAPI (para webhook/dashboard futuros), Parquet + SQLite, pytest. |
| Datos ES/NQ | **Databento (GLBX.MDP3)** como fuente autoritativa CME; alternativas: Interactive Brokers, FirstRateData (histórico barato). TradingView NO como fuente de datos inicial. |
| Rollover | Guardar **contratos individuales crudos** + construir **serie continua** documentada (roll por volumen/OI). Niveles (VWAP/ORB/liquidez) sobre el contrato activo; estadística agregada sobre continua ajustada por ratio. |
| Backtester | **Event-driven bar-replay propio**, que alimenta el **mismo** SignalEngine que se usará en vivo → no-lookahead por construcción. NautilusTrader como ruta de migración futura, no para el MVP. |
| TradingView | Integración **futura** vía adaptador FastAPI (webhook in) + dispatcher (webhook/alert out). El núcleo no se toca. |
| Riesgo mayor | Look-ahead bias, overfitting con muestra pequeña, manejo de sesiones/rollover/DST, y licencias/ToS de datos. |

---

## 1. Repositorios analizados

### 1.1 `haigane-ai/n8n-ai-trading-signals`

| Criterio | Evaluación |
|---|---|
| **Qué hace** | Pipeline en **n8n** (orquestación no-code): recibe alertas de TradingView (Pine webhook) → valida *killzones* ICT → análisis con GPT-4 → filtros de riesgo → señal a Telegram. Log en Google Sheets. Broker (Tradovate) marcado como "Fase 2". |
| **Instrumentos** | **NQ, ES, YM** (¡futuros de índices, no crypto!). Roadmap menciona MNQ/MES. Alineado con tu objetivo. |
| **Stack** | n8n + Pine Script + OpenAI GPT-4 + Telegram Bot + Google Sheets. |
| **Mantenimiento** | **Muy bajo**: 1 star, 0 forks, 1 commit. Un solo autor. |
| **Licencia** | **No declarada** → por defecto "todos los derechos reservados". **No se puede reutilizar código legalmente.** |
| **Backtesting** | **No tiene.** Es un flujo en vivo. |
| **Adecuación ES/NQ** | Conceptualmente muy alineada (killzones, confluencia, riesgo, futuros de índices). |
| **Riesgos** | Depende de n8n (motor externo), de GPT-4 en el camino crítico (viola tu requisito §19 de que el núcleo funcione sin IA), sin licencia, sin tests, sin backtest. |

**Veredicto:** **Inspiración conceptual, no código.** Reutilizamos **ideas**: (a) el concepto de *killzone validator* (filtrado por sesión), (b) el patrón de *confluence scoring* como capa configurable, (c) el patrón webhook→validación→riesgo→alerta. **Evitar:** su arquitectura no-code (n8n), la dependencia de LLM en el camino crítico, y copiar código (sin licencia).

### 1.2 `ranjan98/tradingview-mcp`

| Criterio | Evaluación |
|---|---|
| **Qué hace** | Servidor **MCP** que expone datos/análisis de TradingView a Claude: screener, ratings técnicos, export de OHLCV (hasta 5.000 barras), backtesting simple (SMA/EMA cross, RSI, Donchian, MACD), análisis SMC/ICT (FVG, order blocks, liquidez), scaffolding de Pine, y automatización de cuenta (Playwright). |
| **Stack** | Python 3.10+, FastMCP, Playwright, `tvdatafeed`. Estructura modular (`screener.py`, `history.py`, `backtest.py`, `pine.py`, `webhook_receiver.py`, ...). |
| **Futuros** | Sí, vía símbolos `ES1!` / `NQ1!` en `CME_MINI`. |
| **Mantenimiento** | Reciente pero incipiente: ~6 commits, 0 stars/forks. |
| **Licencia** | **MIT** → reutilizable. |
| **Advertencia importante** | Usa **endpoints no oficiales** de TradingView y **browser automation** → posible violación de **ToS de TradingView**, y los endpoints pueden **romperse sin aviso**. No apto como fuente de datos autoritativa para backtesting serio. |

**Veredicto:** **Referencia de patrones**, no dependencia core. Útil: (a) el `webhook_receiver.py` como referencia para nuestro adaptador de webhook, (b) la organización modular. **Evitar como fuente de datos de producción** (no oficial, frágil, riesgo ToS). Podría servir como *herramienta de exploración manual* opcional, aislada del núcleo.

### 1.3 Otros repos relevantes encontrados

| Repo | Licencia | Estado | Utilidad |
|---|---|---|---|
| **`joshyattridge/smart-money-concepts`** | **MIT** | **2.000+ stars, 851 forks, activo** | ⭐ **El hallazgo más valioso.** Librería Python de conceptos ICT/SMC: FVG, swing highs/lows, BOS, CHoCH, order blocks, liquidez, previous high/low, sesiones, retracements. Entrada = DataFrame OHLC pandas (`open/high/low/close/volume`). Formateada con Black, con tests. |
| `AhmadMan1991/trading-bot` | Revisar | Nicho | Motor ICT con confluence scoring aditivo (liquidity sweeps, OB, FVG, killzones). Buena referencia de **cómo estructurar el scoring**. |
| `CodeWithUmair/VWAP-EMA-BOT` | Revisar | Nicho | Ejemplo de setup CRT/liquidity sweep + VWAP; referencia conceptual. |
| **NautilusTrader** | LGPL | Muy activo, core en Rust | Framework event-driven multi-activo (incl. futuros). **Ruta de migración futura** para ejecución/live; demasiado pesado para un MVP de solo-señales. |
| **`backtesting.py`** | MIT/AGPL* | Activo | Backtester simple para prototipos/reportes rápidos; limitado en multi-timeframe y matices de futuros. |
| **VectorBT** | Apache/comercial | Activo | Vectorizado (NumPy+Numba), ideal para barrer miles de combinaciones de parámetros. Útil en fase de *research* de parámetros, **no** como motor principal (el vectorizado facilita el look-ahead accidental si no se es cuidadoso). |

> \* Verificar la licencia exacta de cada dependencia antes de fijarla (algunas tienen dual-license con cláusula comercial).

**Conclusión sobre `smart-money-concepts`:** es la pieza reutilizable de mayor valor. Es exactamente la "librería de estructura de mercado" que necesitaríamos construir, ya madura y probada. La **envolveremos detrás de nuestra propia interfaz** (`FeatureEngine` / `MarketStructure`) para: (1) no acoplar el núcleo a una dependencia externa, (2) controlar el "as-of bar" y evitar look-ahead, (3) poder sustituirla o complementarla sin reescribir el motor.

---

## 2. Respuestas a las 12 preguntas

### 2.1 ¿Cuál repo utilizarías como base?

**Ninguno como base monolítica.** Recomiendo un **ensamblaje best-of-breed**:

- **Núcleo (Signal/Risk/Backtest/Data): construido por nosotros**, siguiendo tu arquitectura modular del §6. Esto es deliberado: el valor y el edge están en el núcleo, y los repos existentes o no tienen licencia (n8n), o son frágiles (tradingview-mcp como datos), o no cubren el núcleo.
- **`smart-money-concepts` (MIT) como librería de detección** de estructura ICT/SMC, envuelta tras nuestras interfaces.
- **Patrones conceptuales** (no código) del repo n8n para killzones y confluence scoring.

Motivo: cumples tu propio requisito §5 ("no copiar ciegamente") y §19 (el núcleo no depende de IA), y evitas deuda técnica y riesgos legales.

### 2.2 ¿Qué partes reutilizarías?

1. **`smart-money-concepts`** → detección de FVG, BOS, CHoCH, order blocks, swings, liquidez (envuelto en adaptador).
2. **`pandas-ta`** (o `ta`) → RSI, MACD, EMA, ATR, VWAP (indicadores clásicos, bien probados; evita reimplementar).
3. **Patrón killzone/sesión** del repo n8n (idea, reimplementado).
4. **Patrón webhook receiver** de tradingview-mcp (referencia para el adaptador futuro).
5. **Especificaciones de contrato CME** (multiplicadores, ticks) — desde documentación oficial, no de código de terceros.

### 2.3 ¿Qué partes construirías desde cero?

- **DataEngine** + modelo de futuros (contratos, expiración, rollover, sesiones RTH/ETH, timezones).
- **FeatureEngine** (orquesta indicadores + estructura, con garantía "as-of").
- **SetupEngine** (detección de condiciones de setup).
- **SignalEngine** (régimen → setup → confirmación → trigger → señal tipada con razones).
- **RiskEngine** (sizing de futuros, límites, veto).
- **Backtester event-driven** (mismo motor que en vivo → no-lookahead por construcción) + métricas.
- **Paper-signal logger** (registra toda señal y su desenlace).
- **Capa de output/adaptadores** (consola/dashboard ahora; Telegram/TradingView/webhook después).

### 2.4 ¿Qué stack tecnológico recomiendas?

| Capa | Elección | Motivo |
|---|---|---|
| Lenguaje | **Python 3.11+** | Ecosistema cuantitativo, todas las librerías clave, tu equipo puede iterar rápido. |
| Datos/números | **pandas, numpy** | Estándar de facto; formato de entrada de smc. |
| Indicadores | **pandas-ta** (fallback: `ta`) | RSI/MACD/EMA/ATR/VWAP probados. |
| Estructura ICT | **smart-money-concepts** (MIT) | Reutilización de alto valor. |
| Modelos/validación | **pydantic v2** | Señales, config y contratos **tipados** y validados. |
| Config | **YAML + pydantic-settings** | Estrategia y riesgo configurables y transparentes (tu §11/§12). |
| Almacenamiento | **Parquet** (barras) + **SQLite** (señales/paper/backtests) | Rápido, sin servidor, versionable. |
| API/Webhook | **FastAPI + uvicorn** | Para TradingView webhook (in) y API del dashboard (futuro). |
| Tests | **pytest** + **pytest-freezegun** | Incl. tests específicos de no-lookahead y timezones. |
| Dashboard (fase 10) | **React/Next.js** o **Streamlit** para MVP | Backend primero; UI moderna después. |
| Migración futura live | **NautilusTrader** | Event-driven, futuros, ejecución. Solo cuando se aborde ejecución. |

> Alternativa considerada y descartada para el MVP: hacer todo sobre NautilusTrader desde el día 1. Es potente pero añade una curva de aprendizaje y complejidad (core Rust, modelo de dominio propio) innecesaria para un producto de **solo señales**. Lo dejamos como puerta de salida hacia live.

### 2.5 ¿Cómo obtendremos datos de ES/NQ?

**Recomendación principal: Databento (dataset `GLBX.MDP3`, CME Globex MDP 3.0).**

- Fuente **autoritativa y oficial** de CME (no scraping): OHLCV, tick, order book.
- Maneja **rollovers y expiraciones** y ofrece **contratos continuos**.
- Modelo **pay-as-you-go** (medido por bytes) + **$125 de crédito inicial** + plan Standard CME ~$179/mes si se necesita continuo/live.
- Cubre ES, NQ, MES, MNQ (CME/CBOT/NYMEX/COMEX).

**Alternativas / complementos:**

| Fuente | Uso | Notas |
|---|---|---|
| **Interactive Brokers (TWS/IB API)** | Histórico + tiempo real | Requiere cuenta; buena para forward/paper. Límites de rate en histórico. |
| **FirstRateData** | Histórico barato en bulk (15+ años NQ) | Compra única de CSV; bueno para backtest inicial sin costes recurrentes. |
| **Polygon.io** | Alternativa API | Verificar cobertura futuros vs equities. |
| **TradingView (`tvdatafeed`)** | ❌ NO para producción | No oficial, frágil, riesgo ToS. Solo exploración manual aislada. |

**Diseño clave:** una interfaz `MarketDataProvider` abstracta (`get_bars(symbol, timeframe, start, end)`) con implementaciones intercambiables (`DatabentoProvider`, `IBProvider`, `CsvProvider`, `FirstRateProvider`). El núcleo **no sabe** de dónde vienen los datos (cumple §7). Para el MVP: empezar con **CSV/FirstRateData** (barato, offline, reproducible) y añadir Databento cuando se necesite tiempo real.

### 2.6 ¿Cómo manejaremos contratos y rollover?

Los futuros **no son acciones**. El modelo de datos debe representar:

- **`ContractSpec`** por instrumento (verificar contra documentación oficial CME antes de fijar):

| Instrumento | Multiplicador | Tick | Valor tick | 1 punto | Meses | Exchange |
|---|---|---|---|---|---|---|
| **ES** (E-mini S&P 500) | $50 × índice | 0.25 | $12.50 | **$50** | H,M,U,Z | CME |
| **NQ** (E-mini Nasdaq-100) | $20 × índice | 0.25 | $5.00 | **$20** | H,M,U,Z | CME |
| **MES** (Micro E-mini S&P 500) | $5 × índice | 0.25 | $1.25 | **$5** | H,M,U,Z | CME |
| **MNQ** (Micro E-mini Nasdaq-100) | $2 × índice | 0.25 | $0.50 | **$2** | H,M,U,Z | CME |

> ⚠️ Estos valores son los estándar públicos de CME, pero según tu §12 **se verificarán contra la documentación oficial de CME Group antes de escribir el RiskEngine**, y se guardarán en config (no hardcodeados en la lógica).

- **Meses de contrato**: trimestral **H (mar), M (jun), U (sep), Z (dic)**. Expiración típicamente el 3er viernes del mes de contrato.
- **Rollover**: **por volumen/open interest** (el estándar de la industria), ~8 días antes de expiración. Se guarda una `roll_date` por par de contratos.
- **Serie continua**: construimos y guardamos ambas:
  1. **Contratos individuales crudos (sin ajustar)** → para calcular **niveles reales** (VWAP, ORB, PDH/PDL, liquidez) sobre el **contrato activo**. Los niveles de precio ICT **deben** usar precios realmente negociados.
  2. **Continua ajustada por ratio** → para **estadística de rendimiento agregada** en backtest (evita saltos de precio en roll).
- **Sesiones**: modelar **RTH** (09:30–16:00 ET) y **ETH/Globex** (dom 18:00 ET – vie 17:00 ET, con pausa de mantenimiento diaria 17:00–18:00 ET). Todo en **timezone-aware** (`America/New_York`) con manejo explícito de **DST**.

Todo esto se documentará en `MARKET_DATA.md` (tu §24).

### 2.7 ¿Cómo diseñarías el Signal Engine?

Como un **pipeline determinista por etapas**, que produce un objeto `Signal` tipado (o `NoTrade`), separando "condición de mercado" de "oportunidad de trade" (tu §10):

```
MarketState (as-of bar N)
  → RegimeClassifier      → {bullish, bearish, sideways}   (contexto HTF)
  → SetupDetector         → {liquidity_sweep, ORB, ...}     (¿hay un patrón?)
  → ConfirmationCheck     → {vwap_reclaim, volume, ...}     (¿se confirma?)
  → TriggerCheck          → break of short-term structure   (¿gatillo ahora?)
  → (Signal candidate con reasons[])
  → RiskEngine.validate() → aprueba / veta
  → Signal | NoTrade
```

Características:

- **`Signal`** (pydantic) contiene todo lo del §1: instrumento, dirección, entry, SL, TP1/TP2, R:R, stop distance, contratos sugeridos, riesgo en $, `reasons: list[str]`, `invalidation: list[str]`, timeframe, timestamp, confidence.
- **`NoTrade`** es un resultado **de primera clase** con sus `reasons` (tu §10/§16). El bot **no está obligado a operar**.
- **Multi-timeframe** (tu §8): el motor recibe un `MarketState` que agrega HTF (contexto, p.ej. 1H), estructura (15m) y ejecución (5m); los timeframes son **config**, no código.
- **Explicabilidad (§16)**: cada etapa **anexa razones**; la señal se auto-explica y se registra.
- **Sin IA en el camino crítico (§19)**: el motor es 100% cuantitativo/determinista. La IA será una capa **opcional** de filtrado/clasificación de régimen/explicación, desactivable.

**Sobre scoring (§11):** recomiendo implementarlo, pero como **capa configurable y transparente**, no un "AI confidence" inventado. Un scoring aditivo ponderado (trend/structure/liquidity/vwap/volume/momentum/RR) mapea confluencia → `confidence %` y da un umbral claro para NO TRADE. **Condición:** los pesos viven en config, cada componente reporta su aporte, y se **valida en backtest** que el score correlaciona con el resultado (si no aporta edge, se simplifica). No se implementa "porque está escrito", sino tras confirmarlo empíricamente.

### 2.8 ¿Cómo diseñarías el Risk Engine?

Módulo **totalmente separado** de la generación de señales (tu §12), que recibe una señal candidata + estado de cuenta y devuelve aprobación/veto + sizing:

- **Entradas**: entry, stop (en precio), `ContractSpec`, riesgo por trade (config, en $ o % de cuenta), estado del día (trades ya tomados, P&L, drawdown).
- **Cálculos**:
  - `stop_distance_points = |entry - stop|`
  - `stop_distance_ticks = stop_distance_points / tick_size`
  - `risk_per_contract = stop_distance_ticks × tick_value`
  - `suggested_contracts = floor(max_dollar_risk / risk_per_contract)`
  - `dollar_risk = suggested_contracts × risk_per_contract`
  - `R:R` por cada TP.
- **Límites/vetos (→ NO TRADE)**: riesgo máximo por trade, **riesgo diario**, **máximo de trades/día**, **máximo drawdown**, tamaño de stop mínimo/máximo, R:R mínimo.
- **Resultado**: "señal técnicamente válida pero rechazada por reglas de riesgo" es un **NO TRADE explícito** con razón (tu §12).
- Los multiplicadores y ticks se **verifican contra CME** y viven en config.

### 2.9 ¿Cómo haríamos el backtesting?

**Backtester event-driven de reproducción barra a barra**, que alimenta **exactamente el mismo `SignalEngine`** usado en vivo (esto garantiza no-lookahead **por construcción**, tu §14):

- El motor recibe barras **una a una**; en el bar N solo ve datos ≤ N (velas cerradas, indicadores calculados hasta N, swings confirmados, etc.).
- **Modelo de costes realista (tu §13)**: comisión + fees por contrato, **slippage en ticks**, respeta tick size y multiplicador. Nunca reportar resultados sin estos factores.
- **Métricas (tu §13)**: total trades, wins/losses, win rate, avg win/loss, profit factor, expectancy, net P&L, max drawdown, Sharpe, Sortino, rachas, y desgloses por **dirección / instrumento (ES vs NQ) / timeframe / sesión / setup**.
- **Robustez (tu §15)**: **in-sample / out-of-sample**, **walk-forward**, **Monte Carlo** (reordenar/remuestrear trades para estimar rango de drawdown y sensibilidad).
- **Tests anti-lookahead dedicados**: p.ej. inyectar un valor futuro y verificar que el resultado del bar N **no cambia**; comparar señal live-replay vs backtest en el mismo periodo (deben coincidir).

> Para *research* de parámetros a gran escala se puede usar VectorBT en paralelo, pero la **verdad** la da el motor event-driven (menos propenso a look-ahead).

### 2.10 ¿Cómo integraríamos TradingView posteriormente?

Con el **patrón adaptador**, sin tocar el núcleo (tu §3/§21):

**Entrada (TradingView → nosotros):**
```
TradingView (Pine alert) → HTTP POST → FastAPI /webhook/tradingview
  → TradingViewAdapter (normaliza a un evento interno)
  → SignalEngine → RiskEngine → Signal
  → OutputDispatcher
```

**Salida (nosotros → canales):**
```
Signal → OutputDispatcher →
  - Consola / Dashboard (ahora)
  - Telegram / Discord / Email / TradingView webhook / Mobile (después)
```

Claves de diseño:
- Interfaces `InputAdapter` y `OutputChannel` desde el día 1 (aunque solo implementemos consola/dashboard). Añadir TradingView o Telegram = **nueva implementación**, cero cambios en el núcleo.
- El webhook incluirá **validación** (firma/secreto) y normalización; la lógica central es indiferente al origen.
- Se documentará en `TRADINGVIEW_INTEGRATION.md`.

### 2.11 ¿Qué riesgos técnicos existen?

| Riesgo | Severidad | Mitigación |
|---|---|---|
| **Look-ahead bias** | 🔴 Alta | Motor único live/backtest, tests anti-lookahead, "as-of bar" estricto, wraps de smc controlados. |
| **Overfitting** | 🔴 Alta | IS/OOS, walk-forward, Monte Carlo, pocos parámetros, preferir robustez a resultado perfecto. |
| **Sesiones/timezones/DST** | 🟠 Media-alta | Todo timezone-aware (`America/New_York`), tests de sesión/DST, RTH vs ETH explícitos. |
| **Rollover mal manejado** | 🟠 Media-alta | Contratos crudos + continua ratio, roll por volumen, niveles sobre contrato activo, tests de rollover. |
| **Datos: calidad/licencia/coste** | 🟠 Media | Databento oficial; validación de gaps/outliers; evitar scraping de TradingView (ToS). |
| **Muestra pequeña / edge ilusorio** | 🟠 Media | Paper-signals prolongado, expectancy con IC, no concluir con pocos trades. |
| **Dependencias frágiles** | 🟡 Baja-media | Backtrader "congelado" (evitar); smc envuelto tras interfaz; pinear versiones; endpoints no oficiales fuera del núcleo. |
| **Licencias** | 🟡 Baja | Solo reutilizar MIT/permisivas; n8n repo sin licencia = solo ideas. |
| **IA en camino crítico** | 🟡 Baja | Núcleo determinista; IA opcional y desactivable (§19). |

### 2.12 ¿Qué información necesitas que decidamos antes de empezar?

Ver **§4 (Decisiones abiertas)**. Las críticas para arrancar la Fase 2/3:

1. **Presupuesto de datos** (¿FirstRateData/CSV barato para MVP, o Databento de pago desde ya?).
2. ¿Tienes **cuenta de Interactive Brokers** (para forward/paper y datos)?
3. **Tamaño de cuenta y riesgo por trade** (para el RiskEngine: ¿$ fijo o % de cuenta? ¿riesgo diario máx? ¿máx trades/día?).
4. **RTH-only** o incluir **ETH** en el análisis del MVP.
5. **Timeframes prioritarios** (propuesta: 1H contexto / 15m estructura / 5m entrada).
6. Confirmar **Python** como lenguaje.
7. **Setups a priorizar primero** (propuesta MVP: *liquidity sweep + VWAP reclaim + ORB*).
8. Preferencia de **dashboard** (Streamlit rápido vs React/Next pulido) — no bloquea el backend.

---

## 3. Arquitectura y estructura de proyecto propuestas

Alineada con tu §6 y §22, adaptada tras la investigación. El signal engine vivirá bajo un paquete propio para no mezclarse con la app iOS/PWA existente:

```
signal_engine/
├── data/                 # DataEngine: providers, contratos, rollover, sesiones
│   ├── providers/        #   MarketDataProvider (csv, databento, ib, firstrate)
│   ├── contracts.py      #   ContractSpec (multiplicador, tick, meses)
│   ├── rollover.py       #   roll por volumen/OI, continua ajustada
│   └── sessions.py       #   RTH/ETH, timezones, DST
├── features/             # FeatureEngine
│   ├── indicators.py     #   RSI/MACD/EMA/ATR/VWAP (pandas-ta)
│   └── market_structure/ #   wrapper de smart-money-concepts + niveles
├── setups/               # SetupEngine (liquidity sweep, ORB, ...)
├── strategies/           # composición de régimen+setup+confirmación+trigger
├── signals/              # SignalEngine + modelos pydantic (Signal, NoTrade)
├── risk/                 # RiskEngine (sizing, límites, veto) — separado
├── scoring/              # confluence scoring configurable y transparente
├── backtesting/          # motor event-driven + métricas + WFO/MonteCarlo
├── paper/                # paper-signal logger (registra señal vs desenlace)
├── output/               # OutputDispatcher + channels (console, dashboard...)
├── adapters/             # InputAdapter (webhook/tradingview futuro)
├── api/                  # FastAPI (webhook in + API dashboard) [futuro]
├── config/               # YAML tipado (estrategia, riesgo, instrumentos)
└── ...
tests/                    # incl. no_lookahead, sessions/DST, rollover, sizing
docs/
├── ARCHITECTURE.md
├── MARKET_DATA.md
├── STRATEGY.md
├── RISK_MANAGEMENT.md
├── BACKTESTING.md
└── TRADINGVIEW_INTEGRATION.md
```

**Principios (tus §3/§6):** DataEngine no acoplado a TradingView; RiskEngine separado de la generación de señales; estrategia separada de la interfaz; cada módulo modificable de forma independiente; sin ejecución automática (arquitectura preparada, funcionalidad ausente).

---

## 4. Decisiones abiertas (necesito tu input antes de la Fase 3)

| # | Decisión | Opciones | Recomendación por defecto |
|---|---|---|---|
| 1 | Fuente de datos MVP | CSV/FirstRateData / Databento / IB | **CSV/FirstRateData** para empezar barato y reproducible |
| 2 | Cuenta IBKR disponible | Sí / No | — (afecta forward/paper) |
| 3 | Riesgo por trade | $ fijo / % cuenta | **% de cuenta**, con tope diario |
| 4 | Sesión | RTH-only / RTH+ETH | **RTH-only** en MVP, ETH después |
| 5 | Timeframes | libre | **1H / 15m / 5m** |
| 6 | Lenguaje | Python / otro | **Python 3.11+** |
| 7 | Primer setup | libre | **Liquidity sweep + VWAP reclaim + ORB** |
| 8 | Dashboard | Streamlit / React | Backend primero; **Streamlit** para MVP |

---

## 5. Próximos pasos (según tu orden de trabajo §25)

- ✅ **Fase 1 — Investigación** (este documento).
- ⏭️ **Fase 2 — Diseño de arquitectura**: al aprobar §4, redactar `ARCHITECTURE.md`, `MARKET_DATA.md`, `RISK_MANAGEMENT.md` con las decisiones cerradas y las interfaces (contratos de clases) antes de programar.
- ⏭️ **Fase 3 — Data Engine**: providers + contratos + rollover + sesiones + tests.

> Recordatorio de alcance: el producto inicial **no es un auto-trader**. Es un **sistema inteligente de señales** para ES/NQ que tú ejecutas manualmente. TradingView y ejecución automática son integraciones **futuras**, con la arquitectura ya preparada para recibirlas sin reescribir el núcleo.

---

## Fuentes

- [n8n-ai-trading-signals](https://github.com/haigane-ai/n8n-ai-trading-signals)
- [tradingview-mcp](https://github.com/ranjan98/tradingview-mcp)
- [smart-money-concepts](https://github.com/joshyattridge/smart-money-concepts)
- [The Python Backtesting Landscape (2026)](https://python.financial/)
- [Best Python Backtest Engines 2026 — NautilusTrader vs Backtrader vs VectorBT](https://bullalert.ai/blog/best-python-backtest-engines-2026/)
- [Databento — Futures Market Data](https://databento.com/futures) · [Pricing](https://databento.com/pricing) · [GLBX.MDP3](https://databento.com/datasets/GLBX.MDP3)
- [FirstRateData — Historical NQ Futures](https://firstratedata.com/i/futures/NQ)
- [Back-Adjusted vs. Non-Back-Adjusted Futures Contracts](https://vicitradingsolutions.substack.com/p/back-adjusted-vs-non-back-adjusted)
- [Crosstrade — What is Futures Contract Rollover?](https://crosstrade.io/learn/futures-trading/contract-rollover)
