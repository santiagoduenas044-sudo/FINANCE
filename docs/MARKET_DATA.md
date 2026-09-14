# MARKET_DATA.md

> **Fase 2 — Diseño del DataEngine.** Cómo se obtienen, normalizan y sirven los datos de futuros ES/NQ (y después MNQ/MES) al núcleo, **sin acoplar el núcleo a ningún proveedor**.
> Documentos hermanos: [`ARCHITECTURE.md`](./ARCHITECTURE.md), [`RISK_MANAGEMENT.md`](./RISK_MANAGEMENT.md).
> Fecha: 2026-09-14

---

## 1. Principios

1. **Provider-agnóstico.** El núcleo depende del Protocol `MarketDataProvider`, nunca de un proveedor concreto.
2. **MVP con CSV/FirstRateData** (barato, offline, reproducible). **Databento** se incorpora después como fuente de producción/tiempo real implementando el mismo Protocol.
3. **Los futuros NO son acciones.** Se modelan contratos, expiración, rollover, multiplicadores, tick size/value y sesiones.
4. **No-lookahead en la capa de datos.** Resample y niveles solo usan barras cerradas ≤ instante de decisión.
5. **Timezone-aware siempre** (`America/New_York`), con manejo explícito de DST y de la pausa de mantenimiento diaria.

---

## 2. Interfaz `MarketDataProvider`

```python
from typing import Protocol
import pandas as pd

class MarketDataProvider(Protocol):
    def get_bars(
        self,
        symbol: str,          # "ES", "NQ", "MES", "MNQ"
        timeframe: str,       # "1m","5m","15m","30m","1H","4H" (base ideal: 1m)
        start, end,           # tz-aware
        session: str = "ETH", # "RTH" | "ETH"
        contract: str | None = None,   # None => serie continua; o "ESZ25" contrato explícito
        adjustment: str = "raw",       # "raw" | "ratio"  (ver §5)
    ) -> pd.DataFrame: ...
    # Return: DataFrame index = DatetimeIndex tz-aware (America/New_York),
    #         columnas = ["open","high","low","close","volume"], ordenado ascendente,
    #         sin barras en formación (todas cerradas).

    def list_contracts(self, symbol: str) -> list["ContractInfo"]: ...
```

**Implementaciones previstas:**

| Clase | Etapa | Notas |
|---|---|---|
| `CsvProvider` | MVP | Lee CSVs locales normalizados (esquema §3). Para desarrollo/backtest reproducible. |
| `FirstRateProvider` | MVP | Adaptador para el formato de FirstRateData (histórico intradía barato de NQ/ES). Normaliza a esquema §3. |
| `DatabentoProvider` | Futuro | `GLBX.MDP3` (CME oficial). Histórico + tiempo real; provee continuos y rollover nativos. |
| `IBProvider` | Futuro opcional | Solo si existe cuenta IBKR; para forward/paper. No implementar ahora. |

> El núcleo importa `MarketDataProvider`; **nunca** una clase concreta. La elección del provider vive en config.

---

## 3. Esquema de barra (normalizado)

| Columna | Tipo | Regla |
|---|---|---|
| index (timestamp) | datetime tz-aware `America/New_York` | Marca el **cierre** de la barra (etiquetado a la derecha, ver §4). |
| `open` `high` `low` `close` | float | Precio del **contrato** (no ajustado) salvo que `adjustment="ratio"`. |
| `volume` | int/float | Volumen del contrato/barra. |

Metadatos por dataset (sidecar): `symbol`, `timeframe`, `session`, `contract` o `continuous`, `adjustment`, `source`, `tz`.

---

## 4. Timeframes y resample sin look-ahead

- **TF base recomendado:** `1m` (o el más fino disponible). Todos los TF superiores se derivan por resample determinista.
- **Roles (config, `ARCHITECTURE.md §7`):** `context=1H`, `structure=15m`, `execution=5m`. Ampliable.
- **Regla de resample (crítica):**
  - `label="right"`, `closed="right"`: la barra de 5m con timestamp 09:35 cubre `(09:30, 09:35]` y **solo existe** una vez cerrada a las 09:35.
  - Al construir el `AnalysisContext` en el instante N, se **excluye** cualquier barra cuyo cierre sea > N (nada en formación).
  - El resample respeta límites de sesión (una barra no cruza el corte RTH/ETH ni la pausa de mantenimiento).
- **Test dedicado (`tests/no_lookahead/`):** inyectar datos futuros y verificar que las barras y niveles calculados para N no cambian.

```python
# Diseño de resample (no implementación de estrategia)
def resample(df_1m: pd.DataFrame, timeframe: str, session_boundaries) -> pd.DataFrame:
    # agg: open=first, high=max, low=min, close=last, volume=sum
    # label="right", closed="right", origin alineado a apertura de sesión
    ...
```

---

## 5. Contratos, meses y multiplicadores (ES / NQ / MES / MNQ)

`ContractSpec` vive en **config** (`config/contracts.yaml`), no en la lógica. Valores estándar públicos de CME — **a verificar contra documentación oficial de CME Group antes de fijarlos en el RiskEngine** (requisito del usuario):

| Símbolo | Nombre | Multiplicador | Tick size | Tick value | 1 punto | Meses | Exchange |
|---|---|---|---|---|---|---|---|
| **ES** | E-mini S&P 500 | $50 × índice | 0.25 | $12.50 | **$50** | H, M, U, Z | CME |
| **NQ** | E-mini Nasdaq-100 | $20 × índice | 0.25 | $5.00 | **$20** | H, M, U, Z | CME |
| **MES** | Micro E-mini S&P 500 | $5 × índice | 0.25 | $1.25 | **$5** | H, M, U, Z | CME |
| **MNQ** | Micro E-mini Nasdaq-100 | $2 × índice | 0.25 | $0.50 | **$2** | H, M, U, Z | CME |

- **Códigos de mes:** H=marzo, M=junio, U=septiembre, Z=diciembre (ciclo trimestral). Ej.: `ESZ25` = ES diciembre 2025.
- **Expiración:** típicamente el 3er viernes del mes de contrato (verificar por instrumento).

```python
@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    name: str
    multiplier: float        # $ por punto de índice
    tick_size: float         # 0.25
    tick_value: float        # multiplier * tick_size
    months: tuple[str, ...]  # ("H","M","U","Z")
    exchange: str            # "CME"

@dataclass(frozen=True)
class ContractInfo:
    symbol: str              # "ES"
    contract_code: str       # "ESZ25"
    expiration: "date"
    first_notice: "date | None"
```

---

## 6. Rollover y series continuas

Los precios reales negociados y las estadísticas agregadas tienen necesidades distintas → **guardamos ambas representaciones**:

| Representación | Uso | Ajuste |
|---|---|---|
| **Contratos individuales (crudos)** | Cálculo de **niveles reales**: VWAP, ORB, PDH/PDL, ONH/ONL, liquidez, equal highs/lows. | `raw` (sin ajustar) |
| **Serie continua ajustada por ratio** | **Estadística agregada** de backtest (curva de equity, drawdown) sin saltos artificiales en el roll. | `ratio` |

- **Regla de oro:** los **niveles de precio ICT se calculan sobre el contrato activo real** (precios efectivamente negociados). La continua ajustada es solo para métricas de rendimiento.
- **Criterio de roll:** por **volumen / open interest** (estándar de industria): se rota al contrato siguiente cuando su volumen supera al del front por un umbral, típicamente ~8 días antes de expiración. Se registra una `roll_date` por par.
- **Métodos de ajuste** (config `adjustment`):
  - `raw` (unadjusted): precios históricos reales, con saltos en el roll.
  - `ratio` (ratio-adjusted): ajuste por movimiento porcentual en la fecha de roll → sin saltos, apto para returns.
  - (`absolute`/back-adjusted: contemplado, no default.)

```python
class RolloverPolicy(Protocol):
    def roll_dates(self, symbol: str, contracts: list["ContractInfo"],
                   bars_by_contract: dict[str, pd.DataFrame]) -> list["RollEvent"]: ...

@dataclass(frozen=True)
class RollEvent:
    symbol: str
    from_contract: str      # "ESU25"
    to_contract: str        # "ESZ25"
    roll_time: "datetime"
    ratio: float            # factor para serie ratio-adjusted
```

- **Tests (`tests/rollover/`):** continuidad sin saltos en la continua ajustada; niveles calculados sobre el contrato correcto en fechas cercanas al roll; que no se "vea" el contrato futuro antes del roll (no-lookahead).

---

## 7. Sesiones (RTH + ETH) y niveles por sesión

Arquitectura soporta **RTH + ETH**; ETH se habilita/deshabilita por config, con niveles **separados por sesión**.

| Sesión | Horario (ET) | Notas |
|---|---|---|
| **RTH** (Regular Trading Hours) | 09:30 – 16:00 | Sesión "cash"; base de ORB, VWAP de sesión RTH, niveles del día. |
| **ETH / Globex** (Extended) | Dom 18:00 – Vie 17:00 | Casi 23h/día con **pausa de mantenimiento 17:00 – 18:00 ET**. Base de overnight high/low (ONH/ONL). |

- **Timezone canónica:** `America/New_York` (maneja DST automáticamente). Los datos de entrada se convierten a esta zona al normalizar.
- **Corte de sesión y pausa:** el resample y los agregados no cruzan el corte 17:00 ni la reapertura 18:00. Un día de trading Globex va de 18:00 (día previo) a 17:00.
- **Niveles derivados por sesión** (los calcula `context/levels.py`, no el DataEngine):
  - PDH / PDL — previous day high/low (RTH).
  - ONH / ONL — overnight high/low (ETH).
  - Previous close, previous high/low.
  - VWAP de sesión (reinicia en apertura de la sesión configurada).
  - ORB — opening range (ventana configurable tras apertura RTH, p.ej. primeros 15/30 min).
- **Config:**
  ```yaml
  sessions:
    timezone: America/New_York
    rth: { start: "09:30", end: "16:00" }
    eth:
      enabled: true            # toggle independiente
      start: "18:00"           # día previo
      end: "17:00"
      maintenance_break: { start: "17:00", end: "18:00" }
  ```
- **Tests (`tests/sessions/`):** límites correctos en días con DST (marzo/noviembre), pausa de mantenimiento respetada, ONH/ONL vs PDH/PDL no se mezclan entre sesiones.

---

## 8. Validación y calidad de datos

Antes de servir barras al núcleo, el DataEngine valida (y reporta, no silencia):

- Gaps de tiempo inesperados dentro de sesión.
- Barras duplicadas o fuera de orden.
- Outliers de precio (p.ej. saltos > N ticks sin volumen).
- Volumen cero/negativo.
- Cambios de contrato no acompañados de `RollEvent`.
- Coherencia de tz y de horario de sesión.

Los problemas se registran y, según severidad, se descartan/marcan; **nunca** se “rellenan” silenciosamente de forma que introduzcan look-ahead.

---

## 9. Almacenamiento

| Dato | Formato | Motivo |
|---|---|---|
| Barras históricas (por contrato y por TF) | **Parquet** particionado (`symbol/contract/timeframe/`) | Columnar, rápido, comprimido, versionable. |
| Metadatos de contratos y `RollEvent` | Parquet/JSON sidecar | Reproducibilidad del roll. |
| Señales y paper-log | **SQLite** | Consultas relacionales para dashboard y métricas. |
| Resultados de backtest | SQLite/Parquet | Comparables entre corridas. |

---

## 10. Flujo resumido

```
CSV/FirstRateData (MVP)                 Databento (futuro)
        │                                      │
        ▼                                      ▼
   Normalización (esquema §3, tz America/New_York)
        │
        ├── contratos individuales (raw)  ─────────────▶ niveles ICT (VWAP/ORB/PDH/PDL/liquidez)
        │
        └── RolloverPolicy → serie continua (ratio) ───▶ estadística agregada de backtest
        │
        ▼
   resample multi-TF sin look-ahead (1m → 5m/15m/1H)
        │
        ▼
   AnalysisContext "as-of N" (htf/structure/execution)  → resto del pipeline
```

---

## 11. Trade-offs

| Decisión | Alternativa | Coste aceptado |
|---|---|---|
| Guardar crudo **y** continuo | Solo continuo | Más almacenamiento y lógica de roll; a cambio, niveles ICT correctos y estadística sin saltos. |
| `1m` como TF base | Empezar en 5m | Más datos que gestionar; a cambio, resample flexible y niveles finos. |
| Ratio-adjusted como default estadístico | Back-adjusted (absoluto) | Ratio preserva returns porcentuales; absoluto puede dar precios negativos en históricos largos. |
| CSV/FirstRateData primero | Databento ya | Sin tiempo real hasta añadir el provider; interfaz ya preparada, migración sin tocar el núcleo. |
| Zona canónica `America/New_York` | UTC interno | ET es la referencia natural de sesiones CME e ICT; DST se delega a la tz oficial. |
