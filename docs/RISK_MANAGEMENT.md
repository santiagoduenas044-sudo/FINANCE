# RISK_MANAGEMENT.md

> **Fase 2 — Diseño del Risk Engine.** Módulo **separado** de la generación de señales que dimensiona, aplica límites y puede **vetar** una señal (→ `NO_TRADE`).
> Documentos hermanos: [`ARCHITECTURE.md`](./ARCHITECTURE.md), [`MARKET_DATA.md`](./MARKET_DATA.md).
> Fecha: 2026-09-14

---

## 1. Principios

1. **Separación total de la estrategia.** El RiskEngine recibe un `SignalCandidate` ya formado y un `AccountState`; **no** conoce setups ni régimen.
2. **Config-driven, nada hardcodeado.** Modo de riesgo, porcentajes, límites diarios y specs de contrato viven en config.
3. **Dos modos de riesgo por trade** (seleccionables por config):
   - **Porcentaje de cuenta** (`percent`): riesgo = `account_equity × risk_pct`.
   - **Monto fijo** (`fixed`): riesgo = `fixed_dollar`.
4. **El veto es un resultado válido.** "Señal técnicamente válida pero rechazada por reglas de riesgo" → `NO_TRADE` con razón explícita.
5. **Consciente de futuros.** Usa `ContractSpec` (multiplicador, tick size/value) de `MARKET_DATA.md §5`.
6. **Mismo motor en backtest/paper/live** (paridad, sin divergencia).

---

## 2. Defaults para paper/backtest (configurables)

> Valores de arranque, **no** hardcodeados en la estrategia; viven en `config/risk.yaml`.

```yaml
risk:
  mode: percent               # "percent" | "fixed"
  risk_pct: 0.01              # 1% por trade (rango recomendado 0.005–0.01)
  fixed_dollar: 500           # usado sólo si mode == "fixed"
  max_daily_loss_pct: 0.02    # 2% de pérdida máxima diaria (si mode fixed, usar max_daily_loss_dollar)
  max_daily_loss_dollar: null
  max_trades_per_day: 3       # configurable
  min_risk_reward: 1.5        # R:R mínimo al TP1 para no vetar
  max_contracts: 10           # tope duro de tamaño
  min_stop_ticks: 4           # stop demasiado ajustado => veto
  max_stop_ticks: 200         # stop demasiado amplio => veto
  max_drawdown_pct: 0.10      # circuit-breaker de drawdown de la cuenta
  allow_micros: true          # permitir MES/MNQ para sizing fino
```

Rangos por defecto pedidos: **0.5–1% por trade**, **2% pérdida diaria**, **máx trades/día configurable**.

---

## 3. Cálculos (definición formal)

Dado `entry`, `stop`, `ContractSpec` y `AccountState`:

```
stop_distance_points = |entry - stop|
stop_distance_ticks  = stop_distance_points / tick_size
risk_per_contract    = stop_distance_ticks * tick_value        # $ por contrato
                     = stop_distance_points * multiplier        # equivalente

max_dollar_risk      = account_equity * risk_pct               # modo percent
                     |  fixed_dollar                            # modo fixed

suggested_contracts  = floor(max_dollar_risk / risk_per_contract)
suggested_contracts  = min(suggested_contracts, max_contracts)

dollar_risk          = suggested_contracts * risk_per_contract
risk_reward[i]       = |take_profit[i] - entry| / stop_distance_points
```

- **Micros:** si `allow_micros` y el número entero de contratos "grandes" es 0 pero cabría en micros, el motor puede sugerir MES/MNQ para respetar el riesgo objetivo (config).
- **Redondeo:** `suggested_contracts` siempre `floor` (nunca redondear hacia arriba el riesgo).

---

## 4. Ejemplos trabajados (verificar specs contra CME)

**NQ LONG** — cuenta $100.000, `mode=percent`, `risk_pct=1%`:
```
entry 23450, stop 23410  → stop_distance_points = 40
tick_size 0.25, tick_value $5.00 (NQ), multiplier $20
risk_per_contract = 40 * 20 = $800   (= 160 ticks * $5)
max_dollar_risk   = 100000 * 0.01 = $1000
suggested_contracts = floor(1000/800) = 1
dollar_risk = 1 * 800 = $800
TP1 23510 → R:R = 60/40 = 1.5   ✅ (>= min_risk_reward)
TP2 23570 → R:R = 120/40 = 3.0
```

**ES SHORT** — cuenta $50.000, `mode=percent`, `risk_pct=0.5%`:
```
entry 5000.00, stop 5006.00 → stop_distance_points = 6
tick_size 0.25, tick_value $12.50 (ES), multiplier $50
risk_per_contract = 6 * 50 = $300   (= 24 ticks * $12.50)
max_dollar_risk   = 50000 * 0.005 = $250
suggested_contracts = floor(250/300) = 0  → veto por riesgo ES
  → si allow_micros: MES multiplier $5 → risk_per_contract = 6*5 = $30
    suggested = floor(250/30) = 8 MES, dollar_risk = $240  ✅
```

Estos ejemplos ilustran cómo un mismo setup produce `NO_TRADE` en ES pero es viable en MES: el riesgo, no la señal, decide el tamaño.

---

## 5. Límites y vetos (→ NO_TRADE)

El motor evalúa, en orden, y devuelve la **primera** razón de veto (o aprueba):

| Chequeo | Veto si… | Razón NO_TRADE |
|---|---|---|
| R:R mínimo | `risk_reward[TP1] < min_risk_reward` | "R:R insuficiente" |
| Stop mínimo | `stop_distance_ticks < min_stop_ticks` | "stop demasiado ajustado" |
| Stop máximo | `stop_distance_ticks > max_stop_ticks` | "stop demasiado amplio / volatilidad alta" |
| Sizing | `suggested_contracts < 1` (y micros no cubren) | "riesgo por contrato excede el máximo permitido" |
| Trades/día | `trades_today >= max_trades_per_day` | "límite de trades diarios alcanzado" |
| Pérdida diaria | `daily_pnl <= -max_daily_loss` | "límite de pérdida diaria alcanzado" |
| Drawdown | `account_drawdown >= max_drawdown_pct` | "circuit-breaker de drawdown" |

El resultado incluye **todas** las razones evaluadas para trazabilidad (no solo la primera), y el estado que las motivó.

---

## 6. Estado de cuenta y de día

```python
@dataclass(frozen=True)
class AccountState:
    equity: float                 # equity actual
    starting_equity_day: float    # equity al inicio del día de trading
    peak_equity: float            # para drawdown
    daily_pnl: float              # P&L realizado del día
    trades_today: int
    now: "datetime"               # tz-aware; define el "día de trading" (ET, corte de sesión)

@dataclass(frozen=True)
class RiskConfig:
    mode: str                     # "percent" | "fixed"
    risk_pct: float
    fixed_dollar: float | None
    max_daily_loss_pct: float | None
    max_daily_loss_dollar: float | None
    max_trades_per_day: int
    min_risk_reward: float
    max_contracts: int
    min_stop_ticks: int
    max_stop_ticks: int
    max_drawdown_pct: float
    allow_micros: bool
```

- **Definición del "día de trading"**: alineada al corte de sesión de `MARKET_DATA.md §7` (ET). Backtest/paper/live comparten esta definición.
- En **backtest/paper**, `AccountState` lo mantiene el motor de simulación conforme se registran resultados; el RiskEngine es **puro** (mismo input → mismo output).

---

## 7. Interfaz y salida

```python
from enum import Enum

class RiskOutcome(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"      # → se traduce a NoTrade en Validation

@dataclass(frozen=True)
class RiskDecision:
    outcome: RiskOutcome
    suggested_contracts: int
    instrument_used: str          # "NQ" | "MNQ" ... (puede diferir del candidato si micros)
    stop_distance_points: float
    stop_distance_ticks: float
    risk_per_contract: float
    dollar_risk: float
    risk_reward: list[float]
    reasons: list[str]            # aprobación o motivos de veto
    limits_snapshot: dict         # valores de límites y estado al decidir (trazabilidad)

class RiskEngine(Protocol):
    def __init__(self, spec_provider, config: "RiskConfig"): ...
    def validate(self, candidate: "SignalCandidate",
                 account: "AccountState") -> "RiskDecision": ...
```

- El RiskEngine **no** ejecuta ni conecta broker. Solo decide y dimensiona.
- `Validation` (en `signals/`) convierte una `RiskDecision(APPROVED)` + `SignalCandidate` en un `Signal` final, o una `REJECTED` en `NoTrade` con razones.

---

## 8. Relación con el resto del pipeline

```
SignalEngine → SignalCandidate ─┐
                                ├─▶ RiskEngine.validate(candidate, account) → RiskDecision
AccountState (sim/live) ────────┘
                                          │
                                          ▼
                             Validation → Signal | NoTrade → Output / Paper log
```

- Toda decisión (incl. vetos y NO_TRADE) se **registra en el paper log** para medir edge y calidad de las reglas de riesgo.

---

## 9. Trade-offs

| Decisión | Alternativa | Coste aceptado |
|---|---|---|
| RiskEngine **puro** (sin estado propio) | Que el motor guarde el estado del día | Requiere pasar `AccountState` en cada llamada; a cambio es testeable de forma determinista y comparte lógica exacta backtest/live. |
| Modo `percent` y `fixed` por config | Solo porcentaje | Un poco más de config; cubre cuentas y preferencias distintas. |
| Fallback a micros para sizing | Rechazar si no caben contratos grandes | Lógica extra de selección de instrumento; a cambio, viabilidad con cuentas pequeñas y riesgo fino. |
| Veto devuelve **todas** las razones | Solo la primera | Payload algo mayor; a cambio, trazabilidad y mejor análisis del paper log. |
| Límite de drawdown como circuit-breaker | Sin corte global | Puede frenar en rachas malas (deseado); parámetro configurable. |

---

## 10. Fuera de alcance en esta fase

- Implementación del cálculo (solo diseño e interfaces).
- Cualquier envío de órdenes / conexión a broker.
- Gestión de posiciones abiertas o trailing stops en vivo (posible fase futura; el MVP solo sugiere parámetros de la operación que el usuario ejecuta manualmente).
