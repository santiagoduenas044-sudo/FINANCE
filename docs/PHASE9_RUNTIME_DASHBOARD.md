# PHASE9_RUNTIME_DASHBOARD.md — Bot Runtime + Control Dashboard

> Fase 9. `Market Data → Analysis → Setup → Signal → Risk → Runtime →
> Dashboard/Discord`. El dashboard **no modifica la lógica de trading** — solo
> observa `BotRuntime` y controla su ciclo de vida.
> Base: [`PHASE8_BACKTESTING.md`](./PHASE8_BACKTESTING.md) ·
> [`PHASE5_5_DISCORD_OUTPUT.md`](./PHASE5_5_DISCORD_OUTPUT.md).

## 1) Arquitectura

```
DataEngine ──► SetupEngine ──► SignalEngine ──► (opcional) RiskEngine
                                                          │
                                                          ▼
                                              BotRuntime.run_cycle()
                                        (SOLO ejecuta si status == ONLINE)
                                                          │
                              ┌───────────────────────────┼───────────────────┐
                              ▼                            ▼                    ▼
                    recent_signals / events        OutputDispatcher      component_health /
                                                    .publish_signal()      instrument_status
                                                          │
                                                          ▼
                                                DiscordOutputAdapter (Fase 5.5, sin cambios)
                              │
                              ▼
                    FastAPI (signal_engine/api/) ──► GET /api/status, /api/signals/recent,
                                                       /api/risk/today
                                                      POST /api/control/{start,pause,stop}
                              │
                              ▼
                    Dashboard estático (dark mode, responsive) ── solo lectura + 3 botones
```

**El botón ON/OFF controla el runtime real, no solo la UI**: `run_cycle()`
comprueba `self.status` **a sí mismo** y se **niega** a llamar a
Setup/Signal/Risk si no está `ONLINE` (ver `signal_engine/runtime/engine.py`).
Pausar/detener no es una etiqueta visual — cambia lo que la próxima llamada a
`run_cycle()` hace.

## 2) Archivos creados/modificados

**Nuevos:**
- `signal_engine/runtime/{__init__.py, models.py, engine.py}` — `BotRuntime`
- `signal_engine/api/{__init__.py, app.py}` + `signal_engine/api/static/index.html` — FastAPI + dashboard
- `tests/test_bot_runtime.py` (23 tests), `tests/test_api_dashboard.py` (12 tests)
- `scripts/run_dashboard.py`
- `docs/PHASE9_RUNTIME_DASHBOARD.md`

**Modificados (aditivos, sin romper los 24 tests de Discord existentes):**
- `signal_engine/output/events.py`: `OutputEvent.from_signal()` (nuevo, para
  `Signal` — campos distintos a `SetupCandidate`), `OutputEvent.from_runtime_status()`
  (nuevo, para notificaciones de ciclo de vida del bot). `candidate` ahora es
  `Optional` (compatible hacia atrás).
- `signal_engine/output/discord.py`: `render_signal_fields()` +
  renderizado de los dos nuevos tipos de evento (`signal`, `runtime_status`) en
  `build_discord_payload()`. El branch original (`setup_candidate`) **no se
  tocó**.
- `signal_engine/output/dispatcher.py`: `publish_signal()` (nuevo método,
  paralelo a `publish_candidate()` existente).
- `requirements.txt`: `fastapi`, `uvicorn`, `httpx` — ya anticipados como capa
  `api/` futura en `ARCHITECTURE.md`.

**Cero cambios** en `setups/`, `signals/`, `risk/`, `features/`, `data/`
(verificado con `git diff --stat`) — la única interfaz que se tocó fue
`output/`, que es la capa de observabilidad, no de lógica de trading, y ya
estaba diseñada para extenderse (Fase 5.5: "en el futuro podremos tener
DashboardOutputAdapter... sin modificar el Setup Engine").

## 3) `BotRuntime` — estados y control real

```
OFFLINE ──start()──► ONLINE ──pause()──► PAUSED ──start()──► ONLINE
   ▲                    │                                        │
   └──────stop()────────┴─────────────────stop()─────────────────┘
                    (cualquier estado) ──mark_error()──► ERROR ──start()──► ONLINE (recupera)
```

- **`start()`**: `status=ONLINE`, registra `BOT_STARTED`, **envía notificación a
  Discord** (si el adapter está configurado) — vía `OutputEvent.from_runtime_status()`.
- **`pause()`**: `status=PAUSED`. `run_cycle()` se niega a correr — **no se
  generan nuevas señales** (probado explícitamente).
- **`stop()`**: `status=OFFLINE` desde cualquier estado.
- **`mark_error(reason)`**: `status=ERROR`, `last_error` queda **siempre
  visible** (nunca oculto) en `to_status_dict()`. Si `run_cycle()` lanza una
  excepción en cualquier etapa (Data/Analysis/Setup/Signal), se captura, se
  registra en `component_health` con el motivo exacto, y el bot pasa a `ERROR`
  — nunca crashea el proceso.
- **`heartbeat()`**: se actualiza en **cada** llamada a `run_cycle()` (incluso
  si se niega a correr) y también en el loop de fondo cuando está `OFFLINE`/`PAUSED` —
  así el dashboard siempre puede distinguir "el proceso está vivo pero
  pausado" de "el proceso murió".
- **Reloj inyectable** (`now_fn`): permite tests deterministas sin depender
  del reloj real — mismo patrón usado en Setup/Signal/Risk Engine.

## 4) Notificación de Discord al iniciar

`start()` construye `OutputEvent.from_runtime_status("BOT_STARTED", ...)` y lo
envía a través del `OutputDispatcher` existente (Fase 5.5) — **mismo
mecanismo, mismo manejo de errores, mismo `require`/dedup**. Un fallo de
Discord **nunca** impide que `start()`/`pause()`/`stop()` cambien el estado
(probado: `test_discord_failure_does_not_break_lifecycle_control`).

## 5) Seguridad

- **El webhook nunca se expone**: ni en `to_status_dict()`, ni en
  `recent_signals_dict()`, ni en las respuestas HTTP de la API, ni en el HTML
  del dashboard (que solo hace `fetch()` a `/api/*`, nunca conoce el webhook).
  Tests dedicados lo verifican en ambas capas (runtime y API).
- **El dashboard no puede ejecutar órdenes**: no existe ninguna ruta de
  órdenes/ejecución. El único endpoint de control (`POST /api/control/{action}`)
  **valida explícitamente** contra una lista cerrada (`start`, `pause`,
  `stop`) — cualquier otro valor (`execute`, `buy`, `sell`, ...) se rechaza con
  `400`, nunca se interpreta ni ejecuta. Probado.
- **Transiciones inválidas se rechazan**: `pause` cuando no está `ONLINE`
  devuelve `409 Conflict` con el estado actual — nunca "tiene éxito
  silenciosamente" en un estado incoherente. `start`/`stop` son
  intencionalmente idempotentes (reiniciar o detener desde cualquier estado es
  seguro y esperado).
- **Sin autenticación en esta fase** — decisión explícita documentada (ver §7).

## 6) UI — dashboard

Página estática (`signal_engine/api/static/index.html`), sin build ni
frameworks JS nuevos: dark mode, tarjetas con borde sutil, badges de estado
con color+glow (verde ONLINE, amarillo PAUSED, gris OFFLINE, rojo ERROR),
grid responsive (`auto-fit`/`minmax`, colapsa a una columna en móvil), tabla de
señales recientes, salud de componentes en píldoras de color, y un
`error-box` que **muestra el motivo del error** cuando `status=ERROR` (nunca
lo oculta). Polling cada 3s a `/api/status` y `/api/signals/recent` — sin
WebSockets (mantiene la fase simple; preparado para agregar gráficos después
sin rehacer la estructura).

## 7) Decisiones y limitaciones (para tu revisión)

1. **FastAPI + HTML estático**, no Streamlit — la Fase 2 original preveía
   Streamlit como MVP, pero el pedido explícito de esta fase ("interfaz
   moderna/premium... dark mode... preparada para agregar gráficos después")
   encaja mejor con un HTML/CSS/JS propio servido por la capa `api/` que
   `ARCHITECTURE.md` ya anticipaba como "futura". Sin dependencias JS nuevas
   (sin React todavía, tal como se decidió en la Fase 2: reemplazar
   Streamlit/HTML simple por React "después").
2. **`run_cycle()` evalúa la última barra disponible**, no reproduce
   histórico — eso es trabajo del `BacktestEngine` (Fase 8). Mantiene la
   separación pedida.
3. **Sin autenticación** en los endpoints de control — aceptable para uso
   local/desarrollo; **antes de exponer esto en una red no confiable, se
   necesita añadir autenticación** (fuera de alcance, decisión explícita).
4. **Sin persistencia entre reinicios del proceso** — `BotRuntime` vive en
   memoria; si el proceso se reinicia, vuelve a `OFFLINE`. Consistente con
   "no complejidad todavía" de fases anteriores.
5. **`OutputEvent.from_signal`/`from_runtime_status`** son extensiones
   aditivas al diseño de la Fase 5.5 (`candidate` ahora opcional) — no se
   modificó el comportamiento existente para `setup_candidate` (24 tests
   originales intactos).
6. **Sin NQ real en el demo**: no hay datos de muestra de NQ en el repo
   todavía (`tests/data_samples/` solo tiene ES) — el runtime es agnóstico de
   símbolo (`run_cycle(symbol, tf_bars)`), así que soportar NQ es solo cuestión
   de datos, no de código nuevo.

## 8) Tests

`python -m pytest -q` → **277 passed** (242 previos de Fases 3–8 + 23 de
`test_bot_runtime.py` + 12 de `test_api_dashboard.py`). Sin red real (Discord
siempre con transporte simulado).

| Categoría pedida | Test(s) |
|---|---|
| Start | `test_start_transitions_to_online_and_logs_event`, `test_start_endpoint_transitions_status` |
| Pause | `test_pause_transitions_from_online_to_paused`, `test_pause_endpoint_requires_online` |
| Stop | `test_stop_transitions_to_offline_from_any_state`, `test_stop_endpoint_always_succeeds` |
| Estado del bot | `test_initial_status_is_offline`, `test_status_endpoint_returns_offline_initially` |
| Heartbeat | `test_heartbeat_updates_timestamp_deterministically`, `test_run_cycle_always_updates_heartbeat_even_when_refused` |
| Error state | `test_mark_error_sets_error_status_and_reason_visibly`, `test_run_cycle_error_sets_error_status_with_visible_reason`, `test_error_status_appears_in_status_dict` |
| No señales en pausa | `test_run_cycle_refuses_when_paused`, `test_pausing_mid_run_stops_further_cycles_from_acting` |
| Discord startup notification | `test_start_sends_discord_notification`, `test_pause_and_stop_also_notify_discord`, `test_discord_failure_does_not_break_lifecycle_control` |
| Deterministic behavior | `test_deterministic_cycle_results_across_fresh_runtimes`, `test_control_actions_are_deterministic` |

Extra (seguridad): `test_webhook_never_appears_in_status_dict_or_events`,
`test_webhook_never_appears_in_any_api_response`,
`test_no_order_execution_endpoint_exists`, `test_unknown_action_rejected`.

## 9) Cómo iniciar el dashboard

```bash
pip install -r requirements.txt
PYTHONPATH=. python scripts/run_dashboard.py
# abrir http://127.0.0.1:8000/
```

El bot arranca **OFFLINE**. Un hilo de fondo llama a `run_cycle()` cada 5s,
pero **no hace nada** hasta que actives el bot.

## 10) Cómo activar/desactivar el bot

- **Desde la UI**: botones 🟢 Start / 🟡 Pause / 🔴 Stop en el panel "Bot Control".
- **Desde la API** (para scripts/integraciones):
  ```bash
  curl -X POST http://127.0.0.1:8000/api/control/start
  curl -X POST http://127.0.0.1:8000/api/control/pause
  curl -X POST http://127.0.0.1:8000/api/control/stop
  curl http://127.0.0.1:8000/api/status
  ```
- **Notificación a Discord**: si `output.discord.enabled: true` en config y
  `DISCORD_WEBHOOK_URL` está en el entorno, cada start/pause/stop envía un
  mensaje de estado a Discord automáticamente (mismo mecanismo de la Fase 5.5).
