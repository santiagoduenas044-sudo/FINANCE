# PHASE10_LIVE_DATA.md — Live Market Data

> Fase 10. `Live Data → Analysis → Setup → Signal → Risk → Runtime →
> Dashboard/Discord`. Sin ejecución de órdenes.
> Base: [`PHASE9_RUNTIME_DASHBOARD.md`](./PHASE9_RUNTIME_DASHBOARD.md) ·
> [`MARKET_DATA.md`](./MARKET_DATA.md).

## 1) Arquitectura

```
LiveDataAdapter (swappable: Simulated hoy; Databento/IBKR/otro después)
        │  poll(symbol) -> list[LiveBar]   (levanta ConnectionError si falla)
        ▼
    LiveFeed
        │  - valida cada bar entrante (reutiliza validate() de la Fase 3, drop_invalid=True)
        │  - dedup por timestamp (mantiene el valor más reciente)
        │  - re-anota sesión/trading_date (SessionClassifier, Fase 3, sin cambios)
        │  - re-deriva 5m/15m/1H (resample_ohlcv, Fase 3, sin cambios)
        │  - reconexión con reintentos acotados
        │  - health(): LIVE / STALE / DISCONNECTED
        ▼
  get_tf_bars() -> dict[str, DataFrame]      ← MISMO formato que el Data Engine histórico
        │
        ▼
  BotRuntime.run_cycle(symbol)               ← Fase 9, extendida de forma aditiva
        │  (si no se pasa tf_bars, lo toma del LiveFeed configurado)
        ▼
  SetupEngine → SignalEngine → RiskEngine → recent_signals / Discord / Dashboard
```

**Cero cambios** en `setups/`, `signals/`, `risk/`, `features/`, `data/`
(verificado con `git diff --stat`) — se reutiliza exactamente
`SessionClassifier.annotate()`, `resample_ohlcv()` y `validate()` de la Fase
3, y `AnalysisContext`/`analysis_context()` sin ninguna modificación. La
única interfaz tocada fue `runtime/engine.py` (Fase 9), de forma aditiva y
retrocompatible (ver §3).

## 2) Archivos creados/modificados

**Nuevos:**
- `signal_engine/live/{__init__.py, models.py, adapter.py, simulated.py, feed.py}`
- `tests/test_live_data.py` (22 tests)
- `docs/PHASE10_LIVE_DATA.md`

**Modificados (aditivos):**
- `signal_engine/runtime/engine.py`: `run_cycle(symbol, tf_bars=None)` — si se
  omite `tf_bars`, usa `live_feeds[symbol]` (Fase 10); si se pasa
  explícitamente, comportamiento **idéntico** a la Fase 9 (23 tests
  originales de runtime intactos). Nuevo campo `live_feeds` + `data_feed_status`
  (expuesto en `to_status_dict()`).
- `signal_engine/api/static/index.html`: badge LIVE/STALE/DISCONNECTED por
  instrumento.
- `signal_engine/config/schema.py` + `default.yaml`: nueva sección `live:`
  (`stale_after_seconds`, `reconnect_max_attempts`, `reconnect_backoff_seconds`,
  `buffer_max_days`).
- `scripts/run_dashboard.py`: ahora arranca en **modo LIVE** real (via
  `SimulatedLiveAdapter` + `LiveFeed`) en vez de reproducir un diccionario
  histórico estático.

## 3) `LiveDataAdapter` — interfaz intercambiable

```python
class LiveDataAdapter(ABC):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...
    def poll(self, symbol: str) -> list[LiveBar]: ...   # levanta ConnectionError si falla
```

- **No se asume TradingView.** La implementación actual (`SimulatedLiveAdapter`)
  reproduce datos históricos de muestra como si llegaran en vivo — es el MVP,
  análogo a `CsvProvider` en la Fase 3.
- **Para conectar Databento/IBKR/otro después**: solo hace falta una nueva
  clase que implemente `connect()/disconnect()/is_connected/poll()` — **nada
  más cambia** (`LiveFeed`, `BotRuntime`, Setup/Signal/Risk Engine son
  agnósticos de la fuente).

## 4) `LiveFeed` — mismo formato que el Data Engine histórico

- Cada `poll()` entrega barras de 1 minuto ya cerradas (etiquetadas al
  cierre, tz-aware) — mismo contrato que `data/ingestion.normalize()`.
- **Validación de datos incompletos**: cada lote entrante pasa por
  `signal_engine.data.validation.validate()` (Fase 3, sin reimplementar) con
  `drop_invalid=True` — filas con `high<low`, precio no-positivo, volumen
  negativo o NaN se **descartan silenciosamente del buffer** (decisión
  explícita: en vivo protegemos el pipeline; en histórico —Fase 3— el default
  es solo reportar, no descartar — ver §7).
- **Duplicados**: si llega dos veces el mismo timestamp, se conserva el
  valor **más reciente** (`duplicated(keep="last")`).
- **Reconstrucción 5m/15m/1H**: `resample_ohlcv()` (Fase 3) se re-ejecuta
  sobre el buffer completo tras cada ingesta — mismo código, mismo contrato
  de no-lookahead (bins etiquetados al cierre).
- **`get_tf_bars()`** devuelve exactamente `{"1m": df, "5m": df, "15m": df,
  "1H": df}` — el mismo `dict[str, DataFrame]` que `DataEngine.build_timeframes()`
  produce en histórico, por eso `BotRuntime`/`AnalysisContext` no necesitaron
  ningún cambio para consumirlo.

## 5) No-lookahead en modo vivo

"Ahora" en modo vivo es, por construcción, "la última barra que ya tenemos"
— nunca hay datos futuros disponibles. `DataEngine.analysis_context()` (sin
cambios) sigue recortando a `<= decision_time`. Test dedicado
(`test_earlier_decisions_unaffected_by_later_arriving_bars`) prueba que
construir un contexto en N, luego dejar llegar más barras "en vivo", y
reconstruir el contexto en el mismo N de nuevo, produce **exactamente el
mismo resultado** — la garantía no depende de que el archivo histórico sea
estático, sigue valiendo con un buffer que crece.

## 6) Desconexión, datos incompletos y reconexión

- **`poll()` de `LiveFeed` nunca lanza excepción** — encapsula los reintentos
  (`reconnect_max_attempts`, backoff configurable) y, si se agotan, retorna
  `[]` silenciosamente, dejando `health()` en `DISCONNECTED`.
- **Una desconexión NO transiciona el bot a `ERROR`** — es una condición
  operativa esperada y recuperable (decisión explícita, ver §7), reflejada en
  `component_health["data"]` (`DOWN`) y `data_feed_status[symbol]`
  (`DISCONNECTED`), mientras el bot permanece `ONLINE` operando sobre el
  último buffer conocido.
- **Reconexión**: al volver a estar disponible, el siguiente `poll()` exitoso
  limpia el error y el estado vuelve a `LIVE` automáticamente.
- **Heartbeat / last_update**: `FeedHealth.last_update_at` se actualiza solo
  cuando llegan datos nuevos válidos; `STALE` se calcula comparando ese
  timestamp contra el reloj actual (`stale_after_seconds`, configurable).

## 7) Decisiones explícitas (para tu revisión)

1. **Fuente MVP = `SimulatedLiveAdapter`** (reproduce CSV de muestra como si
   fuera en vivo) — este entorno no tiene salida de red hacia proveedores
   reales (mismo bloqueo de egress documentado en `CME_VERIFICATION.md` para
   `cmegroup.com`/`discord.com`; no se intentó conectar a Databento/IBKR
   reales por la misma razón). La interfaz queda lista; conectar un vendor
   real es una implementación nueva de `LiveDataAdapter`, no un cambio
   arquitectónico.
2. **Desconexión ≠ `ERROR` del bot** — se decidió que un feed caído es un
   estado de datos (`DISCONNECTED`), no una falla del pipeline. El bot sigue
   `ONLINE`, listo para reanudar en cuanto el feed vuelva. Si prefieres que
   tras N ciclos desconectado el bot escale a `ERROR`, es un cambio pequeño
   y aislado a `BotRuntime._pull_live_bars`.
3. **Validación en vivo descarta (`drop_invalid=True`) por defecto**, a
   diferencia del histórico (Fase 3, `drop_invalid=False` por defecto, solo
   reporta) — decisión deliberada: en producción en vivo, un dato corrupto no
   debe contaminar indicadores/estructura; en backtesting histórico, se
   prefiere visibilidad total antes de descartar nada.
4. **`run_cycle(symbol, tf_bars=None)` es retrocompatible** — pasar `tf_bars`
   explícitamente (Fase 9) sigue funcionando idéntico; omitirlo activa el
   camino en vivo (Fase 10). Ambos modos comparten el mismo motor.
5. **Sin persistencia del buffer** — igual que `BotRuntime` (Fase 9), el
   buffer de `LiveFeed` vive en memoria; se pierde si el proceso se
   reinicia. Consistente con "sin complejidad todavía".
6. **CME `verified=false` sigue bloqueando todo cálculo monetario/sizing**
   — no se tocó `risk/` ni `contracts.yaml`; el `RiskEngine` (si se conecta
   al runtime) sigue llamando a `require_verified()` sin bypass, exactamente
   igual que en la Fase 7.

## 8) Tests

`python -m pytest -q` → **299 passed** (277 previos de Fases 3–9 + 22 nuevos
de Fase 10). Sin red real.

| Categoría pedida | Test(s) |
|---|---|
| Datos válidos | `test_poll_returns_closed_bars_in_canonical_shape`, `test_get_tf_bars_matches_historical_schema` |
| Barras 5m/15m/1H | `test_5m_15m_1h_bars_are_correctly_aggregated` |
| Timestamps | `test_timestamps_are_close_labeled_and_tz_aware` |
| Datos duplicados/incompletos | `test_duplicate_bar_delivery_keeps_latest_value`, `test_incomplete_or_invalid_bars_are_dropped_not_corrupting_buffer`, `test_negative_volume_bar_is_dropped` |
| Desconexión/reconexión | `test_disconnect_then_reconnect_recovers`, `test_disconnected_feed_does_not_crash_or_raise`, `test_last_error_cleared_after_successful_recovery` |
| Stale data | `test_health_stale_when_no_fresh_data_for_too_long`, `test_dashboard_shows_stale_status` |
| No-lookahead | `test_earlier_decisions_unaffected_by_later_arriving_bars` |
| Integración con Runtime | `test_run_cycle_pulls_from_live_feed_when_no_tf_bars_given`, `test_dashboard_shows_disconnected_status_via_runtime`, `test_no_data_source_configured_is_refused_cleanly`, `test_pausing_stops_live_pipeline_too` |
| Pipeline completo hasta Dashboard/Discord | `test_full_pipeline_reaches_discord_with_real_signals`, `test_full_pipeline_through_dashboard_api`, `test_webhook_never_appears_in_live_pipeline_outputs` |

## 9) Bug encontrado y corregido durante esta fase

Al integrar `LiveFeed` con `BotRuntime`, una desconexión simulada exponía dos
problemas reales (no cosméticos), corregidos antes de escribir los tests
formales:

1. **`object` dtype en el buffer en vivo**: `pd.concat()` con el buffer
   inicial vacío dejaba las columnas OHLCV en dtype `object` en vez de
   `float64`, lo que rompía `vwap()` (indicador de la Fase 4) con
   `TypeError: cumsum is not supported for object dtype`. Corregido forzando
   `astype("float64")` tras cada ingesta.
2. **El estado `DISCONNECTED` se sobrescribía con `OK`**: `run_cycle` volvía
   a marcar el componente `"data"` como `OK` justo después de construir el
   `AnalysisContext` (que sí tiene éxito, porque opera sobre el buffer ya
   almacenado), ocultando el `DOWN`/`DISCONNECTED` que `_pull_live_bars`
   había fijado correctamente. Corregido: ese reset a `OK` solo ocurre en el
   camino histórico (`tf_bars` explícito), no cuando se usó un `LiveFeed`.

## 10) Cómo iniciar el modo LIVE

```bash
pip install -r requirements.txt
PYTHONPATH=. python scripts/run_dashboard.py
# abrir http://127.0.0.1:8000/
```

Arranca **OFFLINE**, con un `LiveFeed` ya conectado a un `SimulatedLiveAdapter`
(reproduce ES en vivo). Actívalo desde la UI (🟢 Start) o:
```bash
curl -X POST http://127.0.0.1:8000/api/control/start
curl http://127.0.0.1:8000/api/status   # incluye data_feed_status: {"ES": "LIVE"}
```

**Para conectar una fuente real** (Databento/IBKR/otra): implementar
`LiveDataAdapter` para ese vendor y sustituir `SimulatedLiveAdapter` en
`scripts/run_dashboard.py:build_runtime()` — ninguna otra pieza del sistema
cambia.
