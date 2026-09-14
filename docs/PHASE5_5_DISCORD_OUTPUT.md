# PHASE5_5_DISCORD_OUTPUT.md — Discord Output Adapter

> Fase 5.5. Añade Discord como **capa de observabilidad externa** del Setup
> Engine. **No** es un Signal Engine, **no** genera LONG/SHORT finales, **no**
> agrega IA/LLM, riesgo/sizing, ejecución ni TradingView.
> Base: [`PHASE5_SETUP_ENGINE.md`](./PHASE5_SETUP_ENGINE.md).

## 1) Arquitectura

```
SetupEngine.detect(ctx)  ─►  list[SetupCandidate]
                                   │
                                   ▼
                     OutputEvent.from_setup_candidate(c)   (events.py)
                                   │
                                   ▼
                         OutputDispatcher.publish_candidate
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                              ▼ (futuro)
          DiscordOutputAdapter            DashboardOutputAdapter
          TelegramOutputAdapter (futuro)  TradingViewOutputAdapter (futuro)
                    │
                    ▼
          Discord Incoming Webhook (HTTP POST)
```

- **`OutputAdapter`** (`signal_engine/output/base.py`) es la interfaz genérica que
  cualquier canal futuro implementa. `DiscordOutputAdapter` es la primera
  implementación.
- **El core (`SetupEngine`, los tres detectores) no fue modificado y no importa
  nada de `signal_engine.output`.** El cableado Setup Engine → Discord ocurre en
  la capa de orquestación (`scripts/demo_discord_output.py`), nunca dentro del
  motor. Desactivar/eliminar Discord no afecta al core.

## 2) Configurar `DISCORD_WEBHOOK_URL`

```bash
cp .env.example .env
# editar .env y pegar tu Incoming Webhook URL de Discord
```

`.env` está en `.gitignore` — **nunca se commitea**. `config/default.yaml` solo
guarda el **nombre** de la variable de entorno (`webhook_env: "DISCORD_WEBHOOK_URL"`),
nunca el valor.

## 3) Activar / desactivar Discord

En `config/default.yaml`:

```yaml
output:
  discord:
    enabled: false        # true para activar
    webhook_env: "DISCORD_WEBHOOK_URL"
    timeout_seconds: 5.0
    retry_count: 2
    deduplication_enabled: true
```

- `enabled: false` (default) → el adapter nunca se construye/envía; el core no
  se ve afectado.
- `enabled: true` pero sin `DISCORD_WEBHOOK_URL` en el entorno → se detecta,
  se registra un warning, **no se envía nada** (no rompe el pipeline).

## 4) Formato del mensaje

Cada `SetupCandidate` se envía como un embed de Discord:

- Título: **🟡 SETUP CANDIDATE**
- Descripción: *"Setup Candidate — not a final trading signal."* + aclaración de
  que no es una recomendación LONG/SHORT.
- Campos (los que existan): Instrument, Setup, **Bias** (BULLISH/BEARISH — nunca
  LONG/SHORT), Timestamp, Timeframe, Entry, Stop Loss, Take Profit 1, Take
  Profit 2 (si existe), R:R, Market context, Reasons, Referenced liquidity /
  structure, Invalidation, Expiration, Status.

## 5) Comportamiento ante errores

Un fallo de Discord **nunca** detiene el Setup Engine:

| Situación | Comportamiento |
|---|---|
| Discord deshabilitado | `emit()` retorna `False` sin llamar a la red |
| Webhook no configurado | warning en log, `emit()` retorna `False` |
| Timeout | reintento acotado, luego `False` (nunca lanza excepción) |
| HTTP error transitorio (429/500/502/503/504) | reintento con backoff lineal acotado (sin loops infinitos) |
| HTTP error no transitorio (4xx ≠ 429) | se detiene inmediatamente, sin reintentos |
| Excepción de conexión | se captura; se loguea solo el **tipo** de excepción, nunca el mensaje/URL |
| Cualquier adapter lanza una excepción | `OutputDispatcher` la atrapa y continúa |

El webhook **nunca** aparece en logs, mensajes de error, `repr()`/`str()` del
adapter, tests, commits ni documentación (solo se lee de la variable de entorno
en tiempo de ejecución).

## 6) Deduplicación

Cada `SetupCandidate` genera un `dedup_key` **determinista** (SHA-256 de
`instrument | setup_name | timestamp | direction | entry`), sin IDs aleatorios.
El adapter recuerda las claves ya enviadas (en memoria, por proceso) y omite
reenvíos del mismo candidato. Desactivable con `deduplication_enabled: false`.

## 7) Ejecutar los tests

```bash
python -m pytest tests/test_output_discord.py -q
python -m pytest -q     # suite completa
```

Todos los tests usan un **transporte simulado** (`RecordingTransport` /
funciones fake) — **no se realiza ninguna llamada de red real** ni se usa un
webhook real.

## 8) Demo (dry run, sin enviar nada real)

```bash
PYTHONPATH=. python scripts/demo_discord_output.py
```

Sin `DISCORD_WEBHOOK_URL` configurado, detecta candidatos y muestra que el envío
se omite de forma segura.
