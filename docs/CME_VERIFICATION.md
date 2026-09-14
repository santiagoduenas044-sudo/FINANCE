# CME_VERIFICATION.md — Verificación de especificaciones de contrato

> Registro de verificación de `ContractSpec` (ES / NQ / MES / MNQ) contra documentación **oficial de CME Group**,
> según la condición #1 de la Fase 3.

## Estado actual: ⛔ NO VERIFICADO CONTRA CME (bloqueo de red)

**Fecha de intento:** 2026-09-14

**Problema encontrado:** El entorno de ejecución **bloquea el acceso de red a `cmegroup.com`** (todo el dominio) mediante el proxy de egress:

```
EGRESS_BLOCKED: Access to www.cmegroup.com is blocked by the network egress proxy.
EGRESS_BLOCKED: Access to cmegroup.com is blocked by the network egress proxy.
```

Además, la consulta del estado del proxy fue denegada por política del entorno.

**Consecuencia:** No es posible, dentro de este entorno, confirmar los valores directamente contra las páginas oficiales de especificaciones de CME Group. Conforme a la condición #1 (**no asumir valores por memoria ni por fuentes secundarias como verificados**), los `ContractSpec` se han cargado en `signal_engine/config/contracts.yaml` con **`verified: false`** y valores marcados como **PROVISIONALES**.

El código **no** trata estos valores como verificados: `ContractRegistry` expone el flag `verified` y el `RiskEngine` (Fase 7) deberá rechazar operar con specs no verificados.

## Cómo completar la verificación (una de estas opciones)

1. **Habilitar egress a `cmegroup.com`** en el entorno y volver a ejecutar la verificación (puedo automatizarla con WebFetch a las páginas de contract specs).
2. **Pegar/aportar los valores oficiales** desde las páginas de CME Group (enlaces abajo), y yo actualizo `contracts.yaml` con `verified: true`, fuente y fecha.

## Páginas oficiales a consultar (fuente primaria)

- ES — E-mini S&P 500: `cmegroup.com/markets/equities/sp/e-mini-sandp-500.contractSpecs.html`
- NQ — E-mini Nasdaq-100: `cmegroup.com/markets/equities/nasdaq/e-mini-nasdaq-100.contractSpecs.html`
- MES — Micro E-mini S&P 500: `cmegroup.com/markets/equities/sp/micro-e-mini-sandp-500.contractSpecs.html`
- MNQ — Micro E-mini Nasdaq-100: `cmegroup.com/markets/equities/nasdaq/micro-e-mini-nasdaq-100.contractSpecs.html`

## Campos a verificar por instrumento

- Contract size / **multiplier** ($ por punto de índice)
- **Tick size** (mínima fluctuación en puntos de índice)
- **Tick value** ($ por tick) = multiplier × tick_size
- **Meses listados** (ciclo trimestral Mar/Jun/Sep/Dic) y reglas de vencimiento
- **Horarios** (CME Globex ETH y RTH; pausa de mantenimiento)
- Información de **rollover** (último día de negociación, fecha de vencimiento)

## Valores PROVISIONALES cargados (a verificar — NO usar como definitivos)

| Símbolo | Multiplier | Tick size | Tick value | Meses | Origen |
|---|---|---|---|---|---|
| ES | 50 | 0.25 | 12.50 | H,M,U,Z | conocimiento general / pendiente CME |
| NQ | 20 | 0.25 | 5.00 | H,M,U,Z | conocimiento general / pendiente CME |
| MES | 5 | 0.25 | 1.25 | H,M,U,Z | conocimiento general / pendiente CME |
| MNQ | 2 | 0.25 | 0.50 | H,M,U,Z | conocimiento general / pendiente CME |

Horarios provisionales (ET): RTH 09:30–16:00; ETH (Globex) dom 18:00 – vie 17:00 con pausa de mantenimiento diaria 17:00–18:00.

> Estos valores coinciden con los estándar ampliamente publicados, pero **permanecen marcados como no verificados** hasta confirmarlos contra CME por una de las dos vías anteriores.
