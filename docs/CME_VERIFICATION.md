# CME_VERIFICATION.md — Verificación de especificaciones de contrato

> Registro de verificación de `ContractSpec` (ES / NQ / MES / MNQ) contra documentación **oficial de CME Group**.
> Fase 3: primer intento (bloqueado). **Fase 6.5: segundo intento, con más vías probadas — sigue bloqueado.**

## Estado actual: ⛔ NO VERIFICADO CONTRA CME (bloqueo de red, confirmado dos veces)

**`verified: false`** en `signal_engine/config/contracts.yaml`. Ningún valor de
`ContractSpec` (multiplier, tick_size, tick_value, months, horarios) ha sido
confirmado contra la fuente oficial. `ContractRegistry.require_verified()`
sigue lanzando `RuntimeError` para cualquier consumidor (RiskEngine futuro
incluido) que intente operar asumiendo specs verificados — mecanismo intacto y
cubierto por tests (`test_registry_unverified_guard`,
`tests/test_cme_contract_specs.py`).

## Intento 1 — Fase 3 (2026-09-14)

**Método:** `WebFetch` directo a las 4 páginas de contract specs de
`cmegroup.com`.

**Resultado:**
```
EGRESS_BLOCKED: Access to www.cmegroup.com is blocked by the network egress proxy.
EGRESS_BLOCKED: Access to cmegroup.com is blocked by the network egress proxy.
```
Todo el dominio `cmegroup.com` bloqueado. Consulta del estado del proxy
también denegada por política del entorno.

## Intento 2 — Fase 6.5 (2026-09-14, re-verificación previa al Risk Engine)

Antes de construir el Risk Engine se re-intentó la verificación por **tres vías
adicionales**, agotando las opciones razonables dentro de este entorno:

| Vía intentada | Resultado | Por qué no cuenta como verificación |
|---|---|---|
| **`WebFetch` a `cmegroup.com`** (de nuevo, las 4 páginas) | `EGRESS_BLOCKED` — mismo bloqueo de dominio completo que en Fase 3 | No se pudo leer la fuente primaria en absoluto. |
| **`WebFetch` a `web.archive.org`** (copia archivada de las páginas oficiales de CME, como mirror de la fuente primaria) | `Claude Code is unable to fetch from web.archive.org` (host no soportado por la herramienta en este entorno) | No es una señal de red (no es `EGRESS_BLOCKED`); simplemente esta herramienta no puede alcanzar ese host aquí. Sin acceso, no hay lectura verbatim posible. |
| **`WebSearch`** ("CME E-mini S&P 500 contract specs...", "CME Micro E-mini Nasdaq-100...") | Devolvió una **respuesta agregada/parafraseada** que cita cmegroup.com como fuente, coincidiendo numéricamente con los valores provisionales ya cargados | **Deliberadamente NO se usó para marcar `verified: true`.** Es una síntesis de un motor de búsqueda sobre la página, no una lectura directa y citable del HTML/texto oficial de CME. La condición explícita del usuario es "no asumir valores... por fuentes secundarias como verificados", y un resumen de buscador es exactamente eso: una fuente secundaria. |
| **`WebFetch` a `google.com`** (para evaluar si el bloqueo era específico de cmegroup.com o más amplio) | `EGRESS_BLOCKED` también | Confirma que el egress de `WebFetch` en este entorno está restringido a una lista muy acotada de dominios; no es un problema específico de CME. |

**Conclusión del intento 2:** ninguna vía disponible permitió una lectura
primaria verificable de CME Group. **No se modificó ningún valor** de
`contracts.yaml`; se mantienen exactamente los mismos valores provisionales de
la Fase 3 (multiplicador, tick size, tick value, meses) — no inventados ni
"mejorados" con la información no verificable del resumen de búsqueda.

## Exactamente qué dato no pudo verificarse (por instrumento)

| Instrumento | Multiplier | Tick size | Tick value | Point value | Meses | Horarios | Moneda |
|---|---|---|---|---|---|---|---|
| **ES** | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado (= multiplier) | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado (asumido USD) |
| **NQ** | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado |
| **MES** | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado |
| **MNQ** | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado | ⛔ no verificado |

**Point value** = alias de `multiplier` (mismo dato, distinto nombre de uso
común en trading; expuesto como propiedad calculada `ContractSpec.point_value`,
sin duplicar el dato — ver §"Implementación" abajo). Al no estar verificado
`multiplier`, `point_value` tampoco lo está.

**Horarios de trading**: ES, NQ, MES y MNQ comparten el **mismo calendario
CME Globex** (no se duplican por símbolo). Viven centralizados en
`signal_engine/config/schema.py:SessionsConfig` / `config/default.yaml:sessions`
(RTH 09:30–16:00 ET, ETH con pausa de mantenimiento 17:00–18:00 ET) —
**tampoco verificados** contra CME (mismo bloqueo), y explícitamente fuera del
archivo `contracts.yaml` para no duplicar el mismo valor en dos módulos.

## Cómo completar la verificación (sin cambios desde la Fase 3)

1. **Habilitar egress a `cmegroup.com`** (o a un mirror soportado por WebFetch)
   en el entorno y volver a ejecutar la verificación — puedo automatizarla.
2. **Aportar los valores oficiales** copiados directamente desde las páginas
   de CME Group (enlaces abajo) y yo actualizo `contracts.yaml` con
   `verified: true`, `source`, `date`.

## Páginas oficiales a consultar (fuente primaria)

- ES — E-mini S&P 500: `cmegroup.com/markets/equities/sp/e-mini-sandp-500.contractSpecs.html`
- NQ — E-mini Nasdaq-100: `cmegroup.com/markets/equities/nasdaq/e-mini-nasdaq-100.contractSpecs.html`
- MES — Micro E-mini S&P 500: `cmegroup.com/markets/equities/sp/micro-e-mini-sandp-500.contractSpecs.html`
- MNQ — Micro E-mini Nasdaq-100: `cmegroup.com/markets/equities/nasdaq/micro-e-mini-nasdaq-100.contractSpecs.html`

## Campos a verificar por instrumento

- Contract size / **multiplier** ($ por punto de índice) = **point value**
- **Tick size** (mínima fluctuación en puntos de índice)
- **Tick value** ($ por tick) = multiplier × tick_size
- **Moneda** de cotización/liquidación
- **Meses listados** (ciclo trimestral Mar/Jun/Sep/Dic) y reglas de vencimiento
- **Horarios** (CME Globex ETH y RTH; pausa de mantenimiento) — compartidos
  entre los 4 símbolos, verificar una sola vez
- Información de **rollover** (último día de negociación, fecha de vencimiento)

## Valores PROVISIONALES cargados (sin cambios desde la Fase 3 — a verificar, NO usar como definitivos)

| Símbolo | Multiplier (= point value) | Tick size | Tick value | Meses | Origen |
|---|---|---|---|---|---|
| ES | 50 | 0.25 | 12.50 | H,M,U,Z | conocimiento general / pendiente CME |
| NQ | 20 | 0.25 | 5.00 | H,M,U,Z | conocimiento general / pendiente CME |
| MES | 5 | 0.25 | 1.25 | H,M,U,Z | conocimiento general / pendiente CME |
| MNQ | 2 | 0.25 | 0.50 | H,M,U,Z | conocimiento general / pendiente CME |

Horarios provisionales (ET, compartidos): RTH 09:30–16:00; ETH (Globex) dom
18:00 – vie 17:00 con pausa de mantenimiento diaria 17:00–18:00.

> Estos valores coinciden con los estándar ampliamente publicados y con el
> resumen no-primario obtenido por `WebSearch` en la Fase 6.5, pero
> **permanecen marcados como no verificados** hasta confirmarlos contra la
> fuente oficial de CME por una de las dos vías de la sección anterior.

## Implementación (Fase 6.5) — sin duplicar valores

- `ContractSpec` (config/schema.py) gana una **propiedad calculada**
  `point_value` (= `self.multiplier`) para exponer el término de uso común en
  trading sin crear un segundo campo/fuente de verdad.
- `contracts.yaml.verification` ahora registra una **bitácora estructurada de
  intentos** (`attempts: [...]`) y la lista explícita de `unverified_fields`,
  en vez de una sola nota plana — sin tocar ningún valor de spec.
- `ContractRegistry.require_verified()` **no se modificó** (mecanismo de
  bloqueo intacto, ya cubierto por tests desde la Fase 3).
- Los horarios de sesión **no se movieron ni se duplicaron**: siguen
  centralizados en `config/schema.py:SessionsConfig`, compartidos por los 4
  instrumentos.
