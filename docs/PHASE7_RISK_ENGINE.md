# PHASE7_RISK_ENGINE.md — Risk Engine

> Fase 7. `Signal → Risk Validation → Position Size / Risk Decision`.
> **No** ejecuta órdenes, no conecta brokers, no crea dashboard.
> Base: [`RISK_MANAGEMENT.md`](./RISK_MANAGEMENT.md) (diseño Fase 2) ·
> [`CME_VERIFICATION.md`](./CME_VERIFICATION.md) · [`PHASE6_SIGNAL_ENGINE.md`](./PHASE6_SIGNAL_ENGINE.md).

## 1) Arquitectura

```
Signal Engine ──► Signal ──► RiskEngine.evaluate(signal, account) ──► RiskDecision ──► (futuro) Output
                                    │
                    ┌───────────────┼───────────────────────────────┐
                    ▼               ▼                                ▼
        1. registry.require_verified()   2. stop distance > 0   3. sizing + límites diarios
           (gate — sin bypass)              (invalid stop)          (fixed/percent, max_contracts,
                                                                      max_trades_per_day,
                                                                      daily_loss_limit_dollar)
```

`signal_engine/risk/` **no importa nada** de Discord, TradingView, broker,
Streamlit, Ollama o IA/LLM (verificado por grep — solo aparece en
docstrings). Consume `Signal` (Fase 6) y `ContractRegistry`/`ContractSpec`
(Fase 3/6.5) tal como existen, **sin modificarlos**.

## 2) Archivos creados/modificados

**Nuevos:**
- `signal_engine/risk/{__init__.py, models.py, engine.py}`
- `tests/test_risk_engine.py` (27 tests)
- `scripts/demo_risk_engine.py`
- `docs/PHASE7_RISK_ENGINE.md`

**Modificados (aditivos):**
- `signal_engine/config/schema.py`: nueva `RiskConfig` + campo `EngineConfig.risk`.
- `signal_engine/config/default.yaml`: sección `risk:`.

**Cero cambios** en `setups/`, `signals/`, `output/`, `features/`, `data/`
(verificado con `git diff --stat`) — no se modificó ninguna interfaz de
Setup Engine ni Signal Engine; el Risk Engine consume `Signal` tal como se
definió en la Fase 6, sin requerir ningún cambio en él.

## 3) CRÍTICO — CME, sin bypass

El primer paso de `RiskEngine.evaluate()` llama **directamente** a
`registry.require_verified()` (el mecanismo existente desde la Fase 3,
reforzado en la 6.5) dentro de un `try/except`:

```python
try:
    self.registry.require_verified()
except RuntimeError as exc:
    return self._hard_stop(signal, RiskReasonCode.UNVERIFIED_CONTRACT_SPEC, str(exc))
```

- **No se reimplementa ni se debilita el guard** — se invoca el método real.
- Si falla (como ocurre hoy, `verified=false`), el motor **no calcula nada
  monetario**: `risk_per_contract`, `max_risk_dollars`, `dollar_risk` y
  `suggested_contracts` quedan en `0`/`0.0`. La decisión es
  `REJECTED` con `reason_codes=[UNVERIFIED_CONTRACT_SPEC]`.
- Test dedicado (`test_require_verified_is_actually_called_not_reimplemented`)
  fuerza `require_verified()` a fallar vía `monkeypatch` **incluso con un
  registro hipotéticamente verificado**, para probar que el motor realmente
  invoca el método (no una copia de su lógica).
- `contracts.yaml` **no se tocó** en esta fase — sigue con `verified: false`
  y los mismos valores provisionales de la Fase 6.5.

### Cómo se probó el sizing sin verificación real (transparencia total)

Para poder testear la **aritmética** de sizing (obligatorio por la fase:
"sizing ES/NQ/MES/MNQ", "fixed-dollar", "percentage risk", etc.) sin violar
"no bypass de `require_verified()`", los tests de cálculo usan un
`ContractRegistry` **construido en memoria** con `verified=True`
(exactamente el mismo patrón ya usado en
`tests/test_cme_contract_specs.py::test_require_verified_would_pass_once_flag_is_true`
de la Fase 6.5). Esto:

- **Nunca toca `contracts.yaml`** ni cambia el estado real del proyecto.
- **Nunca reclama verificación real de CME** — se documenta explícitamente
  en el código y en los tests (`"NOT a real CME verification"`).
- Permite probar la lógica de negocio (fórmulas, límites, sizing) de forma
  aislada, mientras un test separado (`test_real_registry_is_still_unverified_and_blocks`)
  confirma que el **registro real** sigue bloqueando el cálculo.

Esto no es un bypass: son dos escenarios de prueba distintos con dos inputs
distintos, exactamente como se probaría cualquier código que depende de una
precondición externa.

## 4) Modelo de dominio

```python
class RiskOutcome(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

class RiskReasonCode(str, Enum):
    NONE, UNVERIFIED_CONTRACT_SPEC, UNKNOWN_INSTRUMENT, INVALID_STOP,
    ZERO_RISK_BUDGET, INSUFFICIENT_SIZE, MAX_TRADES_PER_DAY,
    DAILY_LOSS_LIMIT_REACHED, DAILY_LOSS_LIMIT_WOULD_BE_EXCEEDED

@dataclass(frozen=True)
class AccountState:
    equity: float
    daily_realized_pnl: float = 0.0     # signed; negativo = pérdida
    trades_today: int = 0
    daily_realized_risk: float = 0.0    # informativo (ver §7)

@dataclass(frozen=True)
class RiskDecision:
    outcome: RiskOutcome
    instrument: str
    reason_codes: list[RiskReasonCode]
    reasons: list[str]                  # SIEMPRE explicado, se acumulan TODAS las razones
    stop_distance_points: float
    stop_distance_ticks: float
    risk_per_contract: float            # "riesgo por contrato"
    max_risk_dollars: float             # "riesgo máximo por operación"
    suggested_contracts: int            # "contratos permitidos"
    dollar_risk: float                  # "riesgo total"
    risk_reward: list[float]            # del Signal, sin recalcular
    daily_realized_pnl: float           # eco de estado diario
    daily_loss_limit_dollar: float
    trades_today: int
    max_trades_per_day: int
```

## 5) Fórmulas (exactamente como se pidió)

```
stop_distance_points = |entry - stop|
stop_distance_ticks  = stop_distance_points / tick_size
risk_per_contract    = stop_distance_points × point_value      # point_value = multiplier

max_risk_dollars = fixed_risk_dollar                             # modo "fixed"
                 | account.equity × risk_pct                     # modo "percent"
max_risk_dollars = min(max_risk_dollars, max_risk_per_trade_dollar)  # tope opcional adicional

suggested_contracts = min(floor(max_risk_dollars / risk_per_contract), max_contracts)
dollar_risk = suggested_contracts × risk_per_contract
```

- **`floor`, nunca redondeo hacia arriba** — nunca se sobre-arriesga.
- **`max_contracts` es un clamp** (no un rechazo): si el tamaño calculado
  excede el máximo, se recorta y se informa en `reasons` (pero eso solo no
  causa `REJECTED`).
- **`risk_reward`** se copia del `Signal` **sin recalcular** (mismo principio
  de "no homogenizar/reinventar" de fases anteriores).

## 6) Reglas de bloqueo → NO_TRADE (se acumulan todas, no solo la primera)

| Chequeo | Tipo | Efecto |
|---|---|---|
| `require_verified()` falla | **hard-stop** (sin cálculo posterior) | `UNVERIFIED_CONTRACT_SPEC` |
| Instrumento desconocido en el registro | **hard-stop** | `UNKNOWN_INSTRUMENT` |
| `stop_distance_points <= 0` | **hard-stop** | `INVALID_STOP` |
| `max_risk_dollars <= 0` (equity 0 o config inválida) | **hard-stop** | `ZERO_RISK_BUDGET` |
| `suggested_contracts < 1` | acumulable | `INSUFFICIENT_SIZE` |
| `trades_today >= max_trades_per_day` | acumulable | `MAX_TRADES_PER_DAY` |
| `daily_realized_pnl <= -daily_loss_limit_dollar` | acumulable | `DAILY_LOSS_LIMIT_REACHED` |
| `daily_realized_pnl - max_risk_dollars <= -daily_loss_limit_dollar` (y no ya alcanzado) | acumulable | `DAILY_LOSS_LIMIT_WOULD_BE_EXCEEDED` |

Los **hard-stops** cortan inmediatamente (no hay datos válidos con los que
seguir calculando). Las reglas **acumulables** sí permiten seguir calculando
sizing/otros chequeos, y **todas** las que fallen aparecen en `reasons`
— igual que en el patrón ya establecido para `NoTrade` en la Fase 6.

## 7) Daily Risk — estado y alcance

- **`AccountState.daily_realized_pnl`**: P&L realizado del día (con signo;
  negativo = pérdida). Es lo que se compara contra `daily_loss_limit_dollar`.
- **`AccountState.trades_today`**: contador de operaciones ya tomadas hoy.
- **`AccountState.daily_realized_risk`**: campo **informativo** (suma de
  `dollar_risk` ya comprometido hoy) — se expone en el modelo para que una
  capa futura (paper log / dashboard) lo use, pero **no gatea** ninguna
  decisión en esta fase (el único límite diario pedido explícitamente es de
  **pérdida**, no de "riesgo desplegado"; ver §9 decisión #2).
- El Risk Engine **no mantiene estado propio entre llamadas** — recibe
  `AccountState` en cada invocación (igual principio "puro" que Setup/Signal
  Engine). Quien orquesta (backtester, loop de paper/live) es responsable de
  actualizar `AccountState` conforme se cierran operaciones.
- **No se implementó** trailing daily loss ni reglas complejas (drawdown
  circuit-breaker, stop-ticks mínimos/máximos del diseño original de la Fase
  2) — deliberadamente fuera de alcance por instrucción explícita de esta
  fase.

## 8) No-lookahead / determinismo

`RiskEngine.evaluate()` es una **función pura** de `(signal, account, config,
registry)`: sin reloj de pared, sin I/O, sin estado oculto. Tests dedicados:

- **Determinismo**: mismos inputs → misma `RiskDecision`, entre instancias
  frescas del motor y en llamadas repetidas sobre el mismo motor.
- **"No-lookahead" para un motor sin serie temporal**: se interpretó como
  *pureza estricta respecto a lo que se le pasa*. Se probó que evaluar con un
  `AccountState` "posterior" (más pérdida acumulada, más trades) no
  retroactivamente cambia el resultado ya calculado con un `AccountState`
  "anterior" — y que volver a evaluar con el estado "anterior" reproduce
  exactamente el mismo resultado de antes. También se probó que el motor
  nunca muta sus argumentos (`Signal`/`AccountState` de entrada).

## 9) Decisiones explícitas (para tu revisión)

1. **`max_risk_per_trade_dollar`** es un tope **adicional y opcional** sobre
   el modo `fixed`/`percent` (no un tercer modo) — protege cuentas grandes en
   modo `percent` de arriesgar más de un techo absoluto. Por defecto `null`
   (sin efecto).
2. **`daily_loss_limit_dollar` es solo en dólares fijos**, no
   porcentaje-de-cuenta — la instrucción de esta fase solo pidió "daily loss
   limit" como un ítem de configuración (a diferencia del riesgo por
   operación, que sí pidió explícitamente ambos modos). Un límite diario en
   `%` de equity queda como extensión natural futura si se solicita.
3. **Chequeo de "excedería el límite diario"** (`DAILY_LOSS_LIMIT_WOULD_BE_EXCEEDED`):
   además de "ya se alcanzó el límite", el motor también rechaza si el riesgo
   máximo de *esta* operación, sumado a la pérdida ya realizada, cruzaría el
   límite. Esto **no es "trailing daily loss"** (no hay marca de agua móvil
   ni seguimiento dinámico en el tiempo) — es una suma simple, de una sola
   vez, no un estado adicional. Se documenta como decisión explícita por si
   prefieres que el motor solo bloquee **después** de que el límite ya se
   haya cruzado (comportamiento estrictamente más simple, fácil de quitar si
   lo prefieres).
4. **Sin fallback automático a micros**: a diferencia del diseño especulativo
   de la Fase 2 (`allow_micros`), este motor **no** sustituye automáticamente
   ES→MES si el sizing falla. Como el motor ya es genérico por instrumento
   (usa `signal.instrument` contra el registro, sin lógica por símbolo), basta
   con volver a evaluar el mismo `Signal` con `instrument="MES"` para obtener
   el sizing en micros (demostrado en el demo). Mantiene el motor más simple,
   consistente con "no reglas complejas todavía".
5. **`max_contracts` clampa, no rechaza** — igual que el diseño original de la
   Fase 2 (`suggested_contracts = min(..., max_contracts)`), documentado en
   `reasons` cuando efectivamente recorta.

## 10) Tests y resultados

`python -m pytest -q` → **216 passed** (189 previos de Fases 3–6.5 + 27
nuevos de Fase 7). Ningún test usa red.

Cobertura pedida explícitamente, todas presentes en `tests/test_risk_engine.py`:

| Categoría | Test(s) |
|---|---|
| Riesgo por contrato | `test_risk_per_contract_nq_matches_worked_example` |
| Sizing ES/NQ/MES/MNQ | `test_risk_per_contract_es_matches_worked_example`, `test_risk_per_contract_mes_sizes_where_es_could_not`, `test_risk_per_contract_mnq` |
| Fixed-dollar | `test_fixed_dollar_mode_ignores_equity` |
| Percentage risk | `test_percent_mode_scales_with_equity`, `test_max_risk_per_trade_dollar_caps_percent_mode` |
| Max contracts | `test_max_contracts_clamps_large_size` |
| Daily loss limit | `test_daily_loss_limit_already_reached_blocks`, `test_daily_loss_limit_would_be_exceeded_blocks`, `test_daily_loss_limit_not_yet_reached_allows`, `test_positive_daily_pnl_never_blocks` |
| Max trades | `test_max_trades_per_day_blocks`, `test_below_max_trades_per_day_allows`, `test_multiple_reasons_accumulate` |
| Invalid stop | `test_invalid_stop_zero_distance_is_rejected`, `test_negative_distance_is_impossible_by_construction_but_abs_is_safe` |
| Zero/negative risk | `test_zero_equity_percent_mode_is_rejected`, `test_invalid_stop_zero_distance_is_rejected` |
| `verified=false` → bloqueo | `test_real_registry_is_still_unverified_and_blocks`, `test_unverified_block_applies_to_all_four_instruments`, `test_require_verified_is_actually_called_not_reimplemented` |
| Resultados deterministas | `test_same_inputs_produce_identical_decision_across_fresh_engines`, `test_repeated_calls_on_same_engine_are_identical` |
| No-lookahead | `test_decision_depends_only_on_given_account_state_not_hidden_state`, `test_engine_never_mutates_its_inputs` |

Extra: `test_unknown_instrument_is_rejected`, `test_risk_reward_carried_through_unchanged`.

## 11) Limitaciones

- Sin drawdown circuit-breaker, sin stop-ticks mín/máx (diseño Fase 2, no
  pedido en esta fase).
- `AccountState` no persiste — la orquestación (fuera de alcance) debe
  mantenerlo entre operaciones.
- Sin ejecución, sin conexión a broker, sin dashboard — tal como se pidió.
- La verificación CME sigue bloqueada (Fase 6.5); este motor la respeta
  estrictamente y no puede operar con dólares reales hasta que se resuelva.
