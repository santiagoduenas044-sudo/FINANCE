# ARCHITECTURE.md

> **Fase 2 — Diseño de arquitectura** del Signal Engine para futuros de índices (NQ / ES; después MNQ / MES).
> Este documento define **módulos, responsabilidades, interfaces, flujo de datos y contratos de diseño**.
> **No implementa la estrategia** ni ejecución automática. El núcleo es **determinista y funciona sin IA**.
>
> Documentos hermanos: [`MARKET_DATA.md`](./MARKET_DATA.md), [`RISK_MANAGEMENT.md`](./RISK_MANAGEMENT.md). Investigación previa: [`../ARCHITECTURE_RESEARCH.md`](../ARCHITECTURE_RESEARCH.md).
>
> Fecha: 2026-09-14

---

## 1. Principios de diseño (no negociables)

1. **Modularidad estricta.** Cada etapa del pipeline es un módulo con una interfaz explícita y se puede modificar/testear/backtestear de forma aislada.
2. **Estrategia separada de la interfaz.** El núcleo no sabe nada de Streamlit, TradingView, Telegram, ni de qué proveedor de datos se usa.
3. **Risk Engine separado de la generación de señales.** El SignalEngine propone; el RiskEngine dispone (aprueba / veta / dimensiona).
4. **Núcleo determinista sin IA.** Ningún LLM en el camino crítico. La IA será una capa **opcional y desactivable** (clasificación de régimen alternativa, explicaciones enriquecidas), nunca requerida.
5. **Un solo motor de análisis para backtest, paper y live.** El backtester reproduce barras y llama a **exactamente las mismas interfaces**. Esto elimina divergencia y look-ahead **por construcción**.
6. **No-lookahead por contrato.** Toda etapa recibe un contexto "as-of bar N" que **solo** contiene información disponible al cierre de la barra N. Nunca se accede a barras futuras.
7. **`NO TRADE` es un resultado de primera clase.** LONG / SHORT / NO_TRADE son igual de válidos; el sistema no está obligado a operar.
8. **Config-driven.** Timeframes, setups activos, pesos de scoring, parámetros de riesgo y specs de contrato viven en configuración tipada, no en la lógica.
9. **Sin ejecución automática.** La arquitectura prevé un `ExecutionAdapter` futuro, pero **no existe implementación** en esta etapa.

---

## 2. Flujo de datos (pipeline)

```
                         ┌─────────────────── CONFIG (YAML tipado) ───────────────────┐
                         │  timeframes · setups activos · scoring · riesgo · contratos │
                         └──────────────────────────────────────────────────────────────┘
                                                    │ (inyección)
   ┌────────────┐   bars    ┌──────────────┐  features  ┌────────────────────┐
   │  DATA      │──────────▶│  FEATURES    │───────────▶│  MARKET STRUCTURE   │
   │  ENGINE    │  (OHLCV,  │  (indicators)│            │  (BOS/CHoCH/swings) │
   └────────────┘  multi-TF)└──────────────┘            └─────────┬──────────┘
        ▲                                                          │ structure
        │ provider-agnostic                                        ▼
   (CSV/FirstRate hoy,                                   ┌────────────────────┐
    Databento futuro)                                    │ CONTEXT / LIQUIDITY │
                                                         │ (regime, PDH/PDL,   │
                                                         │  VWAP, ORB, sesión) │
                                                         └─────────┬──────────┘
                                                                   │ AnalysisContext (as-of N)
                                                                   ▼
                                                         ┌────────────────────┐
                                                         │  SETUP DETECTION    │  (N detectores
                                                         │  liquidity_sweep,   │   independientes,
                                                         │  vwap_reclaim, orb  │   backtesteables 1×1)
                                                         └─────────┬──────────┘
                                                                   │ SetupResult[]
                                                                   ▼
                                                         ┌────────────────────┐
                                                         │   SIGNAL ENGINE     │  (compone regime+
                                                         │  → SignalCandidate  │   structure+setup+
                                                         │    | NoTrade        │   trigger + scoring)
                                                         └─────────┬──────────┘
                                                                   │ SignalCandidate
                                                                   ▼
                                                         ┌────────────────────┐
                                                         │    RISK ENGINE      │  (sizing futuros,
                                                         │  → RiskDecision     │   límites, veto)
                                                         └─────────┬──────────┘
                                                                   │
                                                                   ▼
                                                         ┌────────────────────┐
                                                         │    VALIDATION       │  (ensambla Signal
                                                         │  → Signal|NoTrade   │   final + razones +
                                                         └─────────┬──────────┘   invalidaciones)
                                                                   │
                                                                   ▼
                                          ┌────────────────────────────────────────────┐
                                          │                 OUTPUT                       │
                                          │  console · dashboard(Streamlit) · paper log  │
                                          │  [futuro] Telegram · Discord · TV webhook     │
                                          └────────────────────────────────────────────┘
```

**Entrada del pipeline** (dos modos, misma interfaz):
- **Backtest / paper**: `BarReplay` emite barras históricas una a una.
- **Live**: un feed en tiempo real emite barras cerradas.
En ambos casos el `AnalysisContext` construido en el bar N es idéntico → sin divergencia.

**Entrada futura por webhook (TradingView)**: un `InputAdapter` normaliza el POST a un evento interno y lo inyecta en el mismo pipeline (§8).

---

## 3. Responsabilidades por módulo

| Módulo | Responsabilidad | NO hace |
|---|---|---|
| **DataEngine** (`data/`) | Cargar OHLCV vía `MarketDataProvider`; resample multi-TF sin look-ahead; contratos, rollover, sesiones RTH/ETH, timezones. | No calcula indicadores ni señales. |
| **FeatureEngine** (`features/`) | Indicadores clásicos (RSI, MACD, EMA, ATR, VWAP) por timeframe. | No decide dirección. |
| **MarketStructure** (`features/market_structure/`) | Swings, BOS, CHoCH, order blocks, FVG (wrapper de `smart-money-concepts`). | No define régimen ni setups. |
| **Context/Liquidity** (`context/`) | Régimen (bull/bear/side), niveles: PDH/PDL, ONH/ONL, VWAP de sesión, ORB, equal highs/lows, sweeps. Separado por sesión. | No genera señales. |
| **SetupEngine** (`setups/`) | Detectores independientes: `LiquiditySweep`, `VwapReclaim`, `ORB`. Cada uno backtesteable por separado. | No dimensiona ni valida riesgo. |
| **SignalEngine** (`signals/`) | Componer régimen + estructura + contexto + setup + trigger; scoring configurable; producir `SignalCandidate` o `NoTrade`. | No calcula contratos ni $ de riesgo. |
| **RiskEngine** (`risk/`) | Sizing de futuros, límites (por trade/diario/trades-día/drawdown), veto → NO_TRADE. Ver `RISK_MANAGEMENT.md`. | No conoce la lógica de setups. |
| **Validation** (`signals/validation.py`) | Ensamblar `Signal` final con `reasons`, `invalidation`, R:R, confidence; o `NoTrade` con razones. | No ejecuta órdenes. |
| **Output** (`output/`) | Despachar a canales (consola, dashboard, paper log). Interfaz `OutputChannel`. | No decide señales. |
| **Adapters** (`adapters/`) | `InputAdapter` (webhook futuro), `ExecutionAdapter` (futuro, no implementado). | — |
| **Backtesting** (`backtesting/`) | `BarReplay`, motor event-driven, costes, métricas, WFO, Monte Carlo. Usa las mismas interfaces del núcleo. | No reimplementa la estrategia. |
| **Paper** (`paper/`) | Registrar toda señal (incl. NO_TRADE) y su desenlace posterior. | No ejecuta. |

---

## 4. Contrato de datos "as-of" (clave anti-lookahead)

El objeto que fluye por el pipeline es un **`AnalysisContext`** construido para un instante de decisión (cierre del bar N del timeframe de ejecución). Reglas:

- Contiene vistas **multi-timeframe** ya recortadas a "≤ N": `htf` (1H), `structure` (15m), `execution` (5m).
- Cada serie está **cerrada**: la última barra incluida es la última **completamente cerrada** en o antes de N (nunca una barra en formación).
- Indicadores, swings, niveles y régimen se calculan **solo** con esas series recortadas.
- El mismo `AnalysisContext` se produce en backtest (desde `BarReplay`) y en live. Un test compara ambos y exige igualdad.

```python
# Pseudo-tipos de diseño (no implementación de estrategia)
from dataclasses import dataclass
from datetime import datetime
import pandas as pd

@dataclass(frozen=True)
class TimeframeView:
    timeframe: str            # "1H", "15m", "5m"
    bars: pd.DataFrame        # OHLCV cerrado, index tz-aware, sólo <= decision_time
    # helpers: last_closed(), lookback(n) ...

@dataclass(frozen=True)
class AnalysisContext:
    symbol: str               # "ES", "NQ"
    decision_time: datetime   # cierre del bar de ejecución (tz-aware, America/New_York)
    session: str              # "RTH" | "ETH"
    htf: TimeframeView        # contexto (1H)
    structure: TimeframeView  # estructura (15m)
    execution: TimeframeView  # entrada (5m)
    # niveles/estructura ya calculados se anexan por el Context/Structure engine
```

---

## 5. Interfaces del núcleo (Protocols)

> Firmas de diseño. La estrategia concreta **no** se implementa aquí.

```python
from typing import Protocol, Sequence, Optional
import pandas as pd

# --- DATA ---
class MarketDataProvider(Protocol):
    def get_bars(self, symbol: str, timeframe: str,
                 start, end, session: str = "ETH") -> pd.DataFrame: ...
    # Devuelve OHLCV tz-aware. Implementaciones: CsvProvider, FirstRateProvider (MVP),
    # DatabentoProvider (futuro). El núcleo depende SÓLO de este Protocol.

# --- FEATURES / STRUCTURE / CONTEXT ---
class FeatureEngine(Protocol):
    def compute(self, view: "TimeframeView") -> "TimeframeView": ...

class MarketStructureAnalyzer(Protocol):
    def analyze(self, view: "TimeframeView") -> "StructureState": ...  # swings, BOS, CHoCH

class ContextEngine(Protocol):
    def build(self, ctx: "AnalysisContext") -> "AnalysisContext": ...  # regime + niveles + liquidez

# --- SETUPS (independientes, backtesteables 1x1) ---
class SetupDetector(Protocol):
    name: str
    def detect(self, ctx: "AnalysisContext") -> Optional["SetupResult"]: ...

# --- SIGNAL ---
class SignalEngine(Protocol):
    def evaluate(self, ctx: "AnalysisContext",
                 setups: Sequence["SetupResult"]) -> "SignalCandidate | NoTrade": ...

# --- RISK (ver RISK_MANAGEMENT.md) ---
class RiskEngine(Protocol):
    def validate(self, candidate: "SignalCandidate",
                 account: "AccountState") -> "RiskDecision": ...

# --- OUTPUT ---
class OutputChannel(Protocol):
    def emit(self, result: "Signal | NoTrade") -> None: ...

# --- ADAPTERS FUTUROS (definidos, NO implementados ahora) ---
class InputAdapter(Protocol):
    def to_event(self, payload: dict) -> "InboundEvent": ...   # TradingView webhook futuro

class ExecutionAdapter(Protocol):
    def submit(self, signal: "Signal") -> None: ...            # broker futuro — NO usar aún
```

---

## 6. Modelos de dominio (salidas tipadas)

```python
from enum import Enum
from dataclasses import dataclass, field

class Direction(str, Enum):
    LONG = "LONG"; SHORT = "SHORT"; NONE = "NONE"

@dataclass(frozen=True)
class SetupResult:
    name: str                     # "liquidity_sweep" | "vwap_reclaim" | "orb"
    direction: Direction
    score: float                  # aporte al scoring (configurable)
    reasons: list[str]
    invalidation: list[str]

@dataclass(frozen=True)
class SignalCandidate:
    symbol: str
    direction: Direction
    timeframe: str
    timestamp: "datetime"
    entry: float
    stop: float
    take_profits: list[float]     # TP1, TP2, ...
    reasons: list[str]
    invalidation: list[str]
    confidence: float             # 0..100, derivado del scoring transparente
    setups: list[str]             # setups que contribuyeron

@dataclass(frozen=True)
class Signal:                     # salida final validada por riesgo
    symbol: str
    direction: Direction          # LONG | SHORT
    timeframe: str
    timestamp: "datetime"
    entry: float
    stop: float
    take_profits: list[float]
    risk_reward: list[float]      # R:R por TP
    stop_distance_points: float
    stop_distance_ticks: float
    suggested_contracts: int
    dollar_risk: float
    reasons: list[str]
    invalidation: list[str]
    confidence: float
    session: str                  # RTH | ETH

@dataclass(frozen=True)
class NoTrade:                    # resultado de PRIMERA CLASE
    symbol: str
    timeframe: str
    timestamp: "datetime"
    reasons: list[str]            # p.ej. "R:R insuficiente", "límite de riesgo diario", "setup incompleto"
```

`RiskDecision` y `AccountState` se especifican en [`RISK_MANAGEMENT.md`](./RISK_MANAGEMENT.md).

---

## 7. Multi-timeframe y setups configurables

- **Roles de timeframe** (config): `context=1H`, `structure=15m`, `execution=5m`. Añadir 4H/30m/1m = cambio de config + provider que sirva ese TF; el núcleo no cambia.
- **Resample sin look-ahead**: el DataEngine agrega desde el TF base con etiquetado a la derecha y ventana cerrada (ver `MARKET_DATA.md §Resample`).
- **Setups independientes**: cada `SetupDetector` se registra en un `SetupRegistry`. El backtester puede activar **uno solo** (`--setup liquidity_sweep`) para medir su edge aislado. Las combinaciones se evalúan **después**, no se asume confluencia simultánea.
- **Separación explícita del flujo estratégico**: `Market Regime → Market Structure → Liquidity/Context → Setup Detection → Signal`. El SignalEngine primero fija régimen y estructura; sólo entonces consulta setups; sólo si hay confluencia suficiente (según scoring/umbral configurable) produce `SignalCandidate`, de lo contrario `NoTrade`.

---

## 8. Integración futura con TradingView (solo interfaz, sin implementar)

**Entrada (TradingView → nosotros):**
```
TradingView (Pine alert) → HTTP POST → FastAPI /webhook/tradingview
   → TradingViewInputAdapter.to_event(payload)  → InboundEvent
   → pipeline (mismo AnalysisContext) → SignalEngine → RiskEngine → Signal
```
**Salida (nosotros → canales):**
```
Signal → OutputDispatcher → OutputChannel(s): console/dashboard (ahora),
                             Telegram/Discord/Email/TV-webhook (futuro)
```
Diseño: `InputAdapter` y `OutputChannel` existen desde ahora como Protocols. Añadir TradingView/Telegram = **nueva clase que implementa el Protocol**, cero cambios en el núcleo. La validación del webhook (secreto/firma) vivirá en la capa `api/`, no en el núcleo. Detalle en un futuro `TRADINGVIEW_INTEGRATION.md`.

> **No se implementa ninguna conexión a TradingView, broker ni ejecución en esta fase.** El `ExecutionAdapter` queda definido pero vacío.

---

## 9. IA: opcional y fuera del camino crítico

- El pipeline §2 es 100% determinista y funciona con la API de IA apagada.
- Puntos de extensión IA **opcionales** (activables por config, con fallback determinista): clasificación de régimen alternativa, filtro de señales, explicación en lenguaje natural, análisis de journal. Ninguno bloquea la generación de señales.

---

## 10. Estructura de proyecto (para la Fase 3+)

```
signal_engine/
├── data/            providers/ · contracts.py · rollover.py · sessions.py · resample.py
├── features/        indicators.py · market_structure/(wrapper smc)
├── context/         regime.py · levels.py · liquidity.py
├── setups/          base.py(registry) · liquidity_sweep.py · vwap_reclaim.py · orb.py
├── signals/         engine.py · scoring.py · validation.py · models.py
├── risk/            engine.py · sizing.py · limits.py · models.py
├── backtesting/     replay.py · engine.py · costs.py · metrics.py · wfo.py · montecarlo.py
├── paper/           logger.py · store.py
├── output/          dispatcher.py · channels/(console.py · dashboard.py)
├── adapters/        input.py(webhook futuro) · execution.py(futuro, no-op)
├── api/             app.py(FastAPI, futuro)
└── config/          schema.py(pydantic) · default.yaml
tests/               no_lookahead/ · sessions/ · rollover/ · sizing/ · setups/ · replay_parity/
docs/                ARCHITECTURE.md · MARKET_DATA.md · RISK_MANAGEMENT.md · (STRATEGY/BACKTESTING/TRADINGVIEW…)
dashboard/           streamlit_app.py (UI, importa SÓLO output/ y stores; nada de lógica)
```

---

## 11. Trade-offs de diseño (decisiones y su coste)

| Decisión | Alternativa descartada | Por qué / coste aceptado |
|---|---|---|
| **Motor event-driven propio, compartido con backtest** | Vectorizado (VectorBT) como motor principal | Elimina look-ahead por construcción y garantiza paridad live/backtest. Coste: más lento para barrer parámetros → VectorBT queda como herramienta de *research* auxiliar, no como verdad. |
| **`AnalysisContext` inmutable "as-of N"** | Pasar DataFrames completos a cada módulo | Previene acceso accidental a futuro. Coste: hay que construir/recortar vistas por barra (más CPU); mitigable con caché incremental. |
| **Envolver `smart-money-concepts` tras interfaz** | Usar la librería directamente en el núcleo | Desacopla y controla el "as-of"; permite sustituirla. Coste: capa adaptadora extra. |
| **Setups como detectores independientes** | Una gran estrategia monolítica | Backtest aislado por setup, claridad. Coste: coordinación de combinaciones se pospone al SignalEngine/scoring. |
| **Scoring configurable y transparente** | Umbral binario simple / “AI confidence” | Da un `confidence` explicable y NO_TRADE claro. Coste: hay que **validar en backtest** que el score aporta edge; si no, se simplifica. |
| **RTH+ETH desde la arquitectura, ETH toggle** | RTH-only y añadir ETH después | Evita reescritura al activar ETH. Coste: manejar la pausa de mantenimiento y niveles por sesión desde ya. |
| **Streamlit para dashboard MVP** | React/Next desde el inicio | Rapidez; lógica desacoplada permite migrar. Coste: UI menos pulida temporalmente. |
| **CSV/FirstRateData primero** | Databento de pago desde el día 1 | Barato, offline, reproducible. Coste: sin tiempo real hasta añadir el provider Databento (interfaz ya lista). |
| **Micros (MES/MNQ) contemplados en specs** | Solo ES/NQ | Permiten sizing más fino con cuentas pequeñas. Coste: mantener 4 `ContractSpec`. |

---

## 12. Qué queda explícitamente fuera de esta fase

- Implementación de cualquier setup / estrategia / scoring concreto.
- Cualquier conexión a broker, TradingView o ejecución de órdenes.
- Cualquier uso de LLM en el camino crítico.
- Backtester funcional (solo su diseño). Se implementa en Fase 8 con las mismas interfaces aquí definidas.
