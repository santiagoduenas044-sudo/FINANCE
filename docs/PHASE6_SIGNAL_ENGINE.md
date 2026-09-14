# PHASE6_SIGNAL_ENGINE.md — Signal Engine

> Fase 6. Convierte `SetupCandidate → Signal / NoTrade`.
> **No** implementa risk/sizing, broker, TradingView, Streamlit, React, Ollama,
> IA/LLM, machine learning, optimización ni ejecución automática.
> Base: [`PHASE5_SETUP_ENGINE.md`](./PHASE5_SETUP_ENGINE.md) · [`PHASE5_5_DISCORD_OUTPUT.md`](./PHASE5_5_DISCORD_OUTPUT.md).

## 1) Arquitectura

```
SetupEngine.detect(ctx)  ─►  list[SetupCandidate]
                                   │
                                   ▼
                    SignalEngine.evaluate_candidates(ctx, candidates)
                                   │
                    ┌──────────────┼──────────────────────┐
                    ▼              ▼                       ▼
             conflict check   per-candidate validation   dedup filter
           (misma barra/     (status, expiración, lado    (signal_id ya
            instrumento,      SL/TP, R:R, entry==close[N]) procesado)
            direcciones
            opuestas)
                    │
                    ▼
                list[Signal | NoTrade]
                                   │
                                   ▼
                         (futuro) Output Dispatcher → Discord
```

`signal_engine/signals/` **no importa nada** de `signal_engine.output` (verificado
por grep: solo aparece en comentarios/docstrings, nunca en un `import`). El
diagrama `Setup Engine → Signal Engine → Output Dispatcher → Discord` se
mantiene como **arquitectura de referencia**; la conexión real de `Signal` hacia
el `OutputDispatcher`/Discord **no se implementó en esta fase** (ver §7 —
decisión explícita para no tocar código ya aprobado de la Fase 5.5 sin que se
pidiera).

## 2) Archivos creados/modificados

**Nuevos:**
- `signal_engine/signals/{__init__.py, models.py, validators.py, conflicts.py, engine.py}`
- `tests/test_signal_engine.py` (23 tests)
- `docs/PHASE6_SIGNAL_ENGINE.md`

**Modificados (aditivos, sin romper nada existente):**
- `signal_engine/config/schema.py`: nueva `SignalConfig` (`min_risk_reward`,
  `deduplication_enabled`, `validate_entry_against_context`) + campo
  `EngineConfig.signals`.
- `signal_engine/config/default.yaml`: sección `signals:`.
- `signal_engine/data/context.py`: método nuevo `AnalysisContext.view_for(timeframe)`
  (busca la vista de rol —execution/structure/htf— que coincide con un
  timeframe; devuelve `None` si no existe). Puramente aditivo; no se tocó
  `setups/_helpers.py` ni la lógica de los tres setups.

**Cero cambios** en `signal_engine/setups/` (detección de Liquidity Sweep, VWAP
Reclaim y ORB intacta) ni en `signal_engine/output/` (Discord intacto).

## 3) Modelo de dominio

```python
class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

# SetupCandidate.direction (BULLISH/BEARISH, bias) -> decisión definitiva
DIRECTION_TO_SIGNAL = {Direction.BULLISH: SignalDirection.LONG,
                       Direction.BEARISH: SignalDirection.SHORT}

@dataclass(frozen=True)
class Signal:
    signal_id: str                 # determinista (sha256), sin aleatoriedad
    instrument: str
    setup: str
    direction: SignalDirection      # LONG / SHORT
    timestamp: pd.Timestamp
    timeframe: str
    entry: float
    stop: float
    take_profits: list[float]       # targets específicos del setup, sin homogeneizar
    risk_reward: list[float]
    reasons: list[str]
    market_context: dict
    invalidation_conditions: list[str]
    invalidation_price: float
    referenced: list[LevelRef]
    expiration: pd.Timestamp | None

@dataclass(frozen=True)
class NoTrade:
    instrument: str
    timestamp: pd.Timestamp
    reasons: list[str]              # SIEMPRE explicado, nunca vacío
    setup: str | None
    timeframe: str | None
    candidate_direction: SignalDirection | None
```

`compute_signal_id(instrument, setup_name, direction, timestamp, timeframe, entry, stop)`
= SHA-256 de esos 7 campos concatenados — **determinista, sin IDs aleatorios**. Se
usa tanto como `Signal.signal_id` como clave interna de deduplicación.

## 4) Reglas aplicadas (en orden, se acumulan todas las razones de fallo)

| Regla | Validador | Efecto si falla |
|---|---|---|
| **Conflicto entre setups** | `conflicts.find_conflicts` | Si ≥2 candidatos del mismo `instrument` en el mismo lote (misma llamada a `detect(ctx)`) tienen direcciones opuestas → **todos** se convierten en `NoTrade` con la razón del conflicto (esto **anula** el resto de validaciones para esos candidatos: un conflicto real hace irrelevante el R:R individual). |
| **Status del candidato** | `validate_status` | `status != VALID` (p.ej. ya `INVALID`/`EXPIRED` por `setups.models.update_status`) → razón `"...status is INVALID/EXPIRED..."`. |
| **Expiración por tiempo** | `validate_not_expired` | `ctx.decision_time > candidate.valid_until` → razón `"...expired..."`. Red de seguridad adicional a `status` (cubre el caso de reevaluar un candidato "viejo" en un contexto posterior). |
| **Lado de SL/TP** | `validate_side` | LONG: `stop >= entry` o algún TP `<= entry` → inválido. SHORT: simétrico. |
| **R:R mínimo** | `validate_risk_reward` | R:R del TP más cercano `< min_risk_reward` (default `1.0`, no optimizado) → inválido. También cubre riesgo cero/negativo. |
| **Entry == close[N]** | `validate_entry_matches_context` | Si `candidate.timeframe` existe en el `AnalysisContext` y la barra `candidate.timestamp` está presente, `candidate.entry` debe coincidir con el `close` de esa barra exacta (tolerancia de punto flotante). Activable/desactivable por config. |

Si **ninguna** regla falla → `Signal` (con `signal_id` determinista). Si **alguna** falla → `NoTrade` con **todas** las razones que fallaron (no solo la primera).

## 5) Reglas específicas por setup

Tal como se pidió, el Signal Engine **no reimplementa ni acopla** la lógica de
detección de cada setup (eso ya vive, correctamente aislado, en
`signal_engine/setups/`):

- **Liquidity Sweep**: una vela, nivel previamente confirmado — ya garantizado
  por `LiquiditySweepDetector` (Fase 5); el Signal Engine solo valida
  invariantes genéricas (lado SL/TP, R:R, entry==close[N]) sobre el candidato
  que ya llega correctamente formado.
- **VWAP Reclaim**: una vela, VWAP de sesión — igual, ya garantizado aguas
  arriba; sin reimplementación.
- **ORB**: RTH, opening range configurable, primera ruptura tras completar el
  OR — igual.
- **Targets específicos de cada setup se mantienen intactos**: `Signal.take_profits`
  es una copia literal de `candidate.take_profits` (Liquidity Sweep usa
  liquidez opuesta + múltiplos R; VWAP Reclaim usa múltiplos R; ORB usa
  movimientos medidos del rango OR) — el Signal Engine no los homogeniza ni
  recalcula.

## 6) No-lookahead

- Todas las funciones de `validators.py` son **puras**: solo leen los campos
  propios del candidato y el `AnalysisContext` ya construido (as-of), nunca
  tocan datos futuros, reloj de pared ni red.
- `validate_entry_matches_context` busca la barra por **su propio timestamp**
  (`candidate.timestamp`), nunca por "última fila" — test dedicado demuestra
  que si el `AnalysisContext` llegara a contener filas posteriores a esa barra
  (lo cual no debería ocurrir dado que `AnalysisContext` siempre se construye
  as-of, pero se verifica defensivamente), el resultado **no cambia**.
- **Test end-to-end** (`test_no_lookahead_end_to_end`): para cada barra de
  decisión N con resultados, se compara `SetupEngine.detect + SignalEngine.evaluate_candidates`
  corriendo sobre el dataset completo (recortado a N) vs. sobre un dataset
  truncado físicamente a N — **resultado idéntico** (mismo Signal/NoTrade,
  mismas razones). Esto prueba que el mismo motor funciona igual en backtest
  (reproducción histórica) y en vivo/paper (evaluación "as-of ahora").
- El motor **no usa estado global de tiempo real** en ningún punto — la única
  mutación interna es el set de deduplicación (`_seen`), que es puramente una
  función de los candidatos ya vistos, no del reloj.

## 7) Decisiones y limitaciones

1. **Conflicto = mismo lote, mismo instrumento, direcciones opuestas.** Se
   define "mismo lote" como el resultado de **una** llamada a
   `SetupEngine.detect(ctx)` (una barra de decisión). Dos setups que coinciden
   en dirección (confluencia) **no** es conflicto — ambos se convierten en
   `Signal` independientes. Esto es una decisión de diseño explícita, no
   verificada empíricamente; **ambigüedad para tu revisión**: ¿debería un
   conflicto entre timeframes distintos del mismo instrumento (p.ej. ORB en 5m
   vs. un futuro setup en 15m) contar igual? Hoy sí (se agrupa solo por
   `instrument`, no por `timeframe`), porque los tres setups actuales comparten
   timeframe (5m) por defecto.
2. **`min_risk_reward` por defecto = 1.0**, valor razonable **sin optimizar**
   (cumple "no optimizar parámetros"). Configurable en `config/default.yaml`.
3. **`validate_entry_matches_context` es "best-effort", no un hard-fail cuando
   no hay información para contrastar** (timeframe fuera de los tres roles, o
   timestamp no presente en la vista) — se decidió así para no romper flujos
   donde el Signal Engine se invoca de forma aislada (como en la mayoría de los
   tests unitarios) sin una `AnalysisContext` completa. Es desactivable
   (`validate_entry_against_context: false`) si se prefiere modo estricto.
4. **Deduplicación es un set en memoria, por instancia de `SignalEngine`** (igual
   patrón que `DiscordOutputAdapter._seen` en la Fase 5.5) — no persiste entre
   procesos. Para backtesting de rangos largos, cada corrida usa una instancia
   fresca; para live/paper de larga duración, se necesitará persistencia
   (fuera de alcance de esta fase).
5. **No se conectó `Signal` al `OutputDispatcher`/Discord.** El diagrama de
   arquitectura pedido (`Setup Engine → Signal Engine → Output Dispatcher →
   Discord`) describe el **flujo objetivo**, pero la tarea no pidió modificar
   `signal_engine/output/` (que ya fue aprobado en la Fase 5.5 con su propio
   contrato `OutputEvent.from_setup_candidate`). Cablear `Signal` al output es
   un cambio pequeño y aislado (`OutputEvent.from_signal(...)`) que se puede
   añadir en una fase futura sin tocar el Signal Engine. **Se deja pendiente de
   tu confirmación** en lugar de asumirlo.
6. **`Signal` no lleva `status`/ciclo de vida** (a diferencia de `SetupCandidate`
   que sí tiene `update_status`). No se pidió explícitamente; se mantiene fuera
   de alcance para no anticipar la futura capa de paper-trading/journal.
7. **Sin sizing ni $**: `risk_reward` sigue siendo solo de precio (como en
   `SetupCandidate`), tal como se exigió ("No agregar risk/sizing").

## 8) Tests y resultados

`python -m pytest -q` → **149 passed** (126 de Fases 3–5.5 + 23 nuevos de
Fase 6). Todos los tests corren sin red (`no internet`).

Cobertura específica pedida, todas presentes en `tests/test_signal_engine.py`:

| Categoría | Test(s) |
|---|---|
| LONG válido | `test_long_valid` |
| SHORT válido | `test_short_valid` |
| SL/TP inválidos | `test_invalid_stop_side_for_long_is_no_trade`, `test_invalid_take_profit_side_for_short_is_no_trade`, `test_zero_risk_distance_is_no_trade` |
| R:R inválido | `test_risk_reward_below_threshold_is_no_trade`, `test_risk_reward_at_or_above_threshold_passes` |
| Expiración | `test_expired_candidate_is_no_trade`, `test_not_yet_expired_candidate_can_be_a_signal`, `test_invalid_or_expired_status_is_rejected` |
| Conflictos | `test_conflicting_setups_produce_no_trade`, `test_agreeing_setups_are_not_a_conflict`, `test_conflict_only_within_same_instrument` |
| Duplicados | `test_duplicate_candidate_is_not_reprocessed`, `test_deduplication_disabled_reprocesses` |
| Deterministic ID | `test_signal_id_is_deterministic_and_no_randomness`, `test_signal_id_differs_by_relevant_field`, `test_evaluate_produces_same_signal_id_across_fresh_engines` |
| NO_TRADE | `test_no_trade_always_has_reasons`, `test_no_trade_accumulates_multiple_reasons` |
| No-lookahead | `test_entry_validation_ignores_bars_after_candidate_timestamp`, `test_no_lookahead_end_to_end`, `test_signal_timestamp_never_after_decision_time` |
