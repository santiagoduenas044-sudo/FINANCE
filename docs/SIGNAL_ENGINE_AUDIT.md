# Auditoría del Motor de Búsqueda de Señales

**Fecha:** 2026-09-20
**Alcance:** Data Engine → Market Analysis → Setup Engine → Signal Engine → GHOST Brain
**Método:** lectura de código + ejecución empírica del pipeline real barra a barra sobre
`tests/data_samples/ES/ESU26_1m.csv` (4 140 barras de 1 m → 828 de 5 m, 276 de 15 m, 72 de 1 H).
Scripts de auditoría reproducibles descritos en el §7.

> **Nada de este documento modifica el código.** Las restricciones del proyecto prohíben tocar la
> lógica de Data/Setup/Signal/Risk Engine. Esto es un diagnóstico: los arreglos propuestos son
> decisiones tuyas, no cambios aplicados.

---

## 1. Respuesta directa: ¿qué usa el bot para buscar señales?

**No usa cruces de EMAs.** Ni como disparador de entrada, ni como filtro de entrada.

Las EMAs 20/50 existen, pero solo alimentan **contexto** (sesgo de timeframe y clasificación de
régimen). Nunca generan una entrada. El disparador de entrada es siempre **acción de precio pura**
sobre un nivel estructural concreto.

Hay exactamente **3 detectores de entrada**, independientes entre sí, todos sobre barras de 5 m cerradas:

| Setup | Disparador exacto (barra N cerrada) | Stop | Targets |
|---|---|---|---|
| **ORB**<br>`setups/orb.py` | El rango de apertura `[09:30, 10:00]` RTH está **completo**, y `close[N]` cruza el `OR_high` (alcista) o el `OR_low` (bajista) viniendo de una barra previa **dentro** del rango | Borde opuesto del OR | `entry ± r × rango_OR` con `r = [1.0, 2.0]` |
| **VWAP Reclaim**<br>`setups/vwap_reclaim.py` | `close[N-1] ≤ VWAP[N-1]` y `close[N] > VWAP[N]` (alcista), o el espejo bajista. VWAP anclado a sesión; ambas barras deben ser de la misma sesión y trading date | Swing opuesto reciente ± buffer de 2 ticks | `entry ± r × riesgo`, `r = [1.0, 2.0]` |
| **Liquidity Sweep**<br>`setups/liquidity_sweep.py` | Un nivel de liquidez **preexistente** (clúster de máximos o mínimos iguales) es perforado por la mecha y el precio **cierra de vuelta** del lado original | Extremo de la mecha del sweep ± buffer | `r = [1.0, 2.0]` + opcionalmente el nivel de liquidez opuesto más cercano |

### La cadena completa de decisión

```
Yahoo 1m (range=5d)
  └─ resample no-lookahead → 5m / 15m / 1H
      └─ MarketAnalyzer: EMA20/50, RSI14, ATR14, VWAP sesión, RVOL20,
         swings (pivotes 3-izq/3-der), BOS/CHoCH, FVG, niveles de liquidez, order blocks
          └─ SetupEngine: los 3 detectores de arriba → SetupCandidate
              └─ SignalEngine: 5 validadores → Signal | NoTrade
                  └─ GhostBrain: 11 guardas duras → scoring de confluencia (umbral 40)
                      └─ APPROVED → PaperTrade → Discord
```

**GHOST no detecta nada.** Solo interpreta y filtra lo que producen los 3 setups. Si los 3 detectores
no disparan, GHOST no tiene nada que aprobar — por muy alineado que esté el mercado.

### Dónde sí entran las EMAs

- `ghost/context.py::_ema_bias` → voto de sesgo: `ema20 - ema50 > 0` = alcista.
- `ghost/regime.py::classify_regime` → TREND_UP / TREND_DOWN / RANGE según `|ema20-ema50|` contra `0.6 × ATR` + pendiente.
- El sesgo por timeframe (`timeframe_bias`) es un **voto de mayoría de 3**: EMA, último evento de estructura, y patrón de swings (HH/HL vs LH/LL). Se necesitan 2 de 3 coincidentes; si no, NEUTRAL.

---

## 2. Resultado empírico del pipeline

Recorriendo las 828 barras de 5 m del histórico de muestra por el pipeline real:

```
1) SETUP ENGINE — candidatos detectados
     liquidity_sweep    271
     vwap_reclaim        47
     orb                 11
                        ───
                        329

2) SIGNAL ENGINE — candidatos → Signal
     APROBADOS: liquidity_sweep 108 · vwap_reclaim 35 · orb 0
     RECHAZADOS:
        58x  liquidity_sweep | R:R bajo el mínimo
        13x  liquidity_sweep | setups en conflicto
         9x  orb             | R:R bajo el mínimo
         8x  vwap_reclaim    | R:R bajo el mínimo
         4x  vwap_reclaim    | setups en conflicto
         2x  orb             | setups en conflicto

3) GHOST — acciones
     NO_TRADE  217
     LONG       18
     SHORT       2

4) GHOST — razones de NO_TRADE
        75x  setup_incomplete
        62x  stop_too_tight
        51x  insufficient_confluence
        19x  conflicting_setups
        12x  excessive_volatility

5) Confluence score (umbral 40.0)
     n=143  min=-31.0  p50=29.0  max=86.0  →  pasan 43 (30 %)
```

**Tasa de conversión global: 329 candidatos → 20 operaciones aprobadas = 6.1 %.**

Esto no es necesariamente malo — un filtro estricto es el objetivo declarado de GHOST. El problema
es *dónde* se pierden y si esas pérdidas son intencionales. Varias no lo son.

---

## 3. Hallazgos confirmados

### 🔴 H-1 — ORB es matemáticamente incapaz de producir una señal

**Severidad: crítica. El setup está muerto y nadie lo sabe.**

En `orb.py::_make`, el stop es el borde opuesto del rango y los targets son múltiplos del **rango
del OR**, no del riesgo:

```python
stop    = or_low  if bullish else or_high
targets = entry ± r × or_range        # r = [1.0, 2.0]
```

Pero la entrada está **fuera** del rango (es un breakout), así que:

```
riesgo = entry − or_low = (entry − or_high) + or_range  >  or_range
premio(TP1) = 1.0 × or_range
R:R(TP1) = or_range / riesgo  <  1.0    SIEMPRE
```

Y `signals.min_risk_reward = 1.0` rechaza todo lo que esté por debajo de 1.0.

Verificado con los valores del propio test del repo (`tests/test_setup_orb.py`):
entry 106, stop 99, TP1 112 → **R:R = 0.857 → rechazado**.

| Overshoot sobre OR_high | entry | riesgo | R:R(TP1) | resultado |
|---|---|---|---|---|
| 0.01 | 105.01 | 6.01 | 0.9983 | rechazado |
| 0.25 | 105.25 | 6.25 | 0.9600 | rechazado |
| 1.00 | 106.00 | 7.00 | 0.8571 | rechazado |
| 3.00 | 108.00 | 9.00 | 0.6667 | rechazado |

**El límite superior de R:R(TP1) es 1.0, alcanzable solo con overshoot = 0** (imposible: la
condición de breakout exige `close > or_high` estrictamente). Empíricamente: **11 candidatos ORB,
11 rechazos, 0 señales — 100 %.**

**Opciones de arreglo (elige una, no las apliqué):**
- **A (mínima, en config):** `setups.orb.rr_targets: [1.5, 3.0]`. Con overshoot típico ≤ 0.5×rango, R:R(TP1) ≈ 1.0–1.5. Sigue siendo frágil.
- **B (recomendada, toca `orb.py`):** medir los targets en múltiplos del **riesgo** como hacen los otros dos setups (`build_targets(entry, stop, rr_targets, bullish)`). Consistente, robusto a cualquier overshoot, y elimina la anomalía de raíz.
- **C:** stop en `or_high − k×ATR` en vez del borde opuesto (un stop de breakout, no de rango completo).

### 🔴 H-2 — El target de liquidez opuesta destruye el R:R del setup dominante

**Severidad: alta. Mata 58 de 271 candidatos de `liquidity_sweep` (21 %).**

`setups.liquidity_sweep.use_opposing_liquidity_target: true` añade el nivel de liquidez opuesto más
cercano a la lista de targets. Pero `_helpers.py::build_targets` **ordena los targets de más cercano
a más lejano sin filtro de distancia mínima**, así que ese nivel se convierte en **TP1** siempre que
esté más cerca que 1R. Y `validate_risk_reward` evalúa **solo TP1**:

```python
nearest_rr = abs(take_profits[0] - entry) / risk   # solo el primero
```

Un nivel de liquidez a 0.05 puntos de la entrada se convierte en un TP1 con R:R = 0.01 y **tumba el
candidato entero**, aunque los targets reales de 1R y 2R sigan ahí intactos.

```
nivel opuesto=5100.05 → TPs=[5100.05, 5105, 5110]  R:R=[0.01, 1.0, 2.0]  → RECHAZADO
nivel opuesto=5101.00 → TPs=[5101.00, 5105, 5110]  R:R=[0.20, 1.0, 2.0]  → RECHAZADO
nivel opuesto=5103.00 → TPs=[5103.00, 5105, 5110]  R:R=[0.60, 1.0, 2.0]  → RECHAZADO
nivel opuesto=5110.00 → TPs=[5105, 5110]           R:R=[1.0,  2.0]       → ok
```

Distribución real del R:R(TP1) de `liquidity_sweep`: **mín 0.010, p50 1.000, máx 1.000** — la
mediana y el máximo son exactamente 1.0 (los targets por R), y toda la cola inferior son niveles de
liquidez intrusos. **43 % de los candidatos caen bajo el umbral.**

**Opciones de arreglo:**
- **A (inmediata, en config, cero código):** `use_opposing_liquidity_target: false`. Recupera ~58 candidatos de golpe.
- **B (mejor, toca `_helpers.py`):** filtrar los `extra_levels` que estén a menos de `min_rr × riesgo` de la entrada antes de mezclarlos.
- **C:** validar el R:R contra el target **más lejano** en vez del más cercano, o contra el mejor de la lista.

### 🟠 H-3 — `min_stop_atr_mult` está calibrado justo en la mediana del setup principal

**Severidad: alta. 62 bloqueos — la guarda dura más activa de GHOST.**

| Setup | Bloqueados | ratio stop/ATR p10 | **p50** | p90 |
|---|---|---|---|---|
| `liquidity_sweep` | **56** | 0.40 | **0.78** | 1.35 |
| `vwap_reclaim` | 6 | 0.74 | 2.08 | 3.49 |

El umbral es `0.80`. La mediana de `liquidity_sweep` es `0.78`. **El umbral cae dentro del ruido de
la distribución que filtra**: mueve el umbral 0.03 y cambias la mitad de las decisiones. Esto no es
un filtro, es un lanzamiento de moneda con pasos extra.

Y es estructural, no accidental: el stop de un liquidity sweep **es la mecha del sweep**. Una mecha
de rechazo mide por naturaleza una fracción del ATR. Pedirle 0.8×ATR es pedirle que deje de ser un
sweep.

**Opciones:** bajar a `0.4`–`0.5`, o hacer el umbral por setup (los setups de mecha necesitan uno
más laxo que los de swing).

### 🟠 H-4 — 16 de los 40 puntos del umbral de confluencia son constantes

**Severidad: alta. El umbral real no es 40, es ~24.**

Frecuencia y aporte medio de cada factor, sobre 143 decisiones puntuadas:

| Factor | Disparos | % | Pts medios | Diagnóstico |
|---|---|---|---|---|
| `volume_confirmation` | 143 | **100 %** | +10.00 | **constante** |
| `rr_quality` | 143 | **100 %** | **+6.00** | **constante, y degradada** |
| `regime_alignment` | 143 | 100 % | **+0.01** | **peso de 10 pts que no hace nada** |
| `liquidity_sweep` | 98 | 69 % | +14.77 | ok |
| `vwap_alignment` | 92 | 64 % | +10.00 | ok |
| `against_structure` | 84 | 59 % | −20.00 | ver H-5 |
| `mtf_structure_alignment` | 57 | 40 % | +15.00 | ok |
| `htf_conflict` | 54 | 38 % | −25.00 | ver H-5 |
| `htf_alignment` | 49 | 34 % | +20.00 | ok |
| `structure_alignment` | 42 | 29 % | +15.00 | ok |

Tres problemas distintos:

1. **`volume_confirmation` dispara en el 100 % de los casos.** `rvol_confirm: 1.0` sobre una media
   móvil de 20 barras debería pasar ~50 % del tiempo. Que pase siempre confirma que sweeps y
   reclaims ocurren por definición en barras de volumen alto — el factor es tautológico respecto a
   los setups. **+10 pts gratis a todo el mundo.**

2. **`rr_quality` vale siempre exactamente +6.00, nunca +12.** El escalón alto exige `R:R ≥ 1.5`,
   pero `build_targets` construye TP1 con `rr_targets[0] = 1.0` **exactamente**. El R:R de TP1 es
   1.000 por construcción, así que solo se alcanza el escalón `≥ 1.0` (mitad de peso). **Un factor
   de 12 puntos permanentemente degradado a 6, con varianza cero.**

3. **`regime_alignment` aporta +0.01 de media.** El régimen casi siempre cae en ramas que devuelven
   factor 0 (`RANGE` genérico o `UNKNOWN`). Un peso declarado de 10 puntos que en la práctica no
   discrimina nada.

**Consecuencia:** 16 puntos (10 + 6) son un offset fijo. Un candidato parte de 16/40 sin haber
demostrado nada. El umbral efectivo de *señal real* es **24 puntos**, no 40 — y el sistema no lo
dice en ninguna parte.

**Arreglo:** subir `rvol_confirm` a ~1.3–1.5 para que el volumen vuelva a discriminar; bajar el
escalón alto de `rr_quality` a `≥ 1.2` o usar el R:R del target más lejano; y o bien revisar
`_regime_fit` o bajar `regime_alignment` a su peso real.

### 🟠 H-5 — Setups de reversión puntuados con un scoring de tendencia

**Severidad: alta. Conflicto de diseño, no un bug de código.**

```
against_structure    84 disparos  (−20 pts)
structure_alignment  42 disparos  (+15 pts)
```

**El bot detecta setups contra la estructura el doble de veces que a favor.** Y es exactamente lo
que debería pasar: `liquidity_sweep` y `vwap_reclaim` son, por definición, setups de **reversión**.
Su tesis es que la estructura reciente está a punto de fallar. Penalizarlos con −20 por ir contra
esa estructura es penalizarlos **por funcionar como están diseñados**.

Lo mismo con `htf_conflict` (54 disparos, −25 pts): un reclaim de VWAP contra-tendencia es un
contra-tendencia *a propósito*.

El scoring ya sabe distinguirlos — `confluence.py` define `_REVERSION_SETUPS` y `_TREND_SETUPS` —
pero **solo usa esa distinción en `_regime_fit`**, que es justo el factor que no hace nada (H-4.3).
Las dos penalizaciones grandes la ignoran por completo.

**Arreglo:** hacer que `against_structure_penalty` y `htf_conflict_penalty` dependan del tipo de
setup — completas para `_TREND_SETUPS`, reducidas o neutras para `_REVERSION_SETUPS` cuando hay
evidencia de agotamiento (CHoCH reciente, sweep reclamado).

### 🟡 H-6 — El sweep que puntúa no tiene ventana de caducidad

**Severidad: media.**

`ghost/context.py::liquidity_state` toma `max(swept, key=swept_at)` — el último sweep **de todo el
histórico en buffer**, sin límite de antigüedad. Medido:

```
antigüedad del sweep que puntúa: p50 = 8 barras (~0.7 h) · p90 = 38 (~3.2 h) · máx = 56 (~4.7 h)
```

En esta muestra de 3 días es tolerable. Con Yahoo a `range=5d` y `buffer_max_days: 10` el máximo
crece proporcionalmente. Peor: `reclaimed` se calcula como "¿el precio está del otro lado del
nivel?" — un sweep de hace 4 horas con el precio 30 puntos arriba da `reclaimed = True`
trivialmente, y **regala +15 pts a cualquier LONG**.

**Arreglo:** ventana de N barras (p. ej. 12–20 de 5 m) sobre `last_sweep_time`, y decaimiento del
peso con la antigüedad.

### 🟡 H-7 — `_ema_bias` no tiene zona muerta

`ghost/context.py`:

```python
d = ema20 - ema50
if d > 0: return BULLISH
if d < 0: return BEARISH
```

Una diferencia de 0.01 puntos en ES produce un voto direccional de pleno derecho. El empate exacto
(NEUTRAL) tiene probabilidad prácticamente nula en flotantes. Como el sesgo de timeframe es un voto
de mayoría de 3, este voto siempre cuenta — incluso cuando las EMAs están efectivamente pegadas.

Irónicamente `regime.py` **sí** lo hace bien: compara contra `range_atr_ratio × ATR`. La misma idea
debería aplicarse aquí.

**Arreglo:** `if abs(d) < k × ATR: return NEUTRAL` con `k ≈ 0.1–0.2`.

### 🟠 H-9 — El registro de auditoría no guarda el desglose de confluencia

**Severidad: alta para la auditabilidad. Descubierto al construir el dashboard.**

`runtime/engine.py::_ghost_log_entry` es un **registro compacto**: aplana el score a un número
y **descarta las contribuciones, el `MarketState` completo y el objeto `Signal`**:

```python
return {
    "kind": "GHOST", "action": ..., "instrument": ..., "setup": ..., "timestamp": ...,
    "regime": ms.regime.regime.value,       # solo la cadena
    "session": ms.session,
    "mtf": ms.mtf.alignment.value,
    "score": decision.confluence.score if decision.confluence else None,   # solo el número
    "no_trade_reasons": [r.value for r in decision.no_trade_reasons],
}
```

Pero `GhostDecision.to_dict()` —que el endpoint `/api/ghost/market/{symbol}` sí devuelve— trae la
estructura completa, contribuciones incluidas. Resultado: **hay dos esquemas distintos para la misma
decisión**, y el que se persiste es el pobre.

Verificado contra el log real generado por el propio runtime sobre el histórico de muestra:

```
decisiones persistidas: 470 · con score: 168 · con desglose de contribuciones: 0
```

**Por qué importa:** el docstring de `/api/ghost/decisions` dice que es *"the honest audit trail —
dashboard shows it so the user can see the filter working"*. Pero de las 87 decisiones rechazadas
por `insufficient_confluence`, **ninguna puede explicar por qué**. Se sabe que el score fue 29 y que
el umbral era 40; no se sabe qué factor faltó. Esa es justo la pregunta que un log de auditoría
existe para responder — y es la que hizo falta reconstruir a mano con scripts para esta auditoría.

**Arreglo:** añadir `"confluence": decision.confluence.to_dict() if decision.confluence else None`
a `_ghost_log_entry`. Es aditivo, no rompe lectores existentes, no toca ninguna decisión de trading,
y cuesta ~400 bytes por decisión puntuada. El dashboard ya lee ambos esquemas, así que el desglose
histórico aparecería solo con hacer ese cambio.

> El dashboard **normaliza los dos esquemas** y dice explícitamente cuándo una decisión no puede
> explicarse retroactivamente, en vez de fingir que el factor valió cero.

### 🟡 H-8 — Hallazgos menores

| # | Archivo | Observación |
|---|---|---|
| a | `ghost/brain.py` | `rr < min_rr` añade **dos** razones (`TARGET_TOO_CLOSE` **y** `INSUFFICIENT_RR`) para una sola causa. Infla las estadísticas de rechazo. |
| b | `ghost/brain.py` | `min_rr: 1.0` duplica exactamente `signals.min_risk_reward: 1.0`. La guarda de GHOST nunca puede dispararse — el Signal Engine ya filtró. Código muerto. |
| c | `signals/engine.py` | `self._seen` crece sin límite. En un proceso de días es una fuga de memoria lenta. Conviene un `deque` acotado o un TTL. |
| d | `signals/validators.py` | `validate_entry_matches_context` compara `entry` con `close[N]`, pero los 3 setups **asignan** `entry = close[N]`. Es un no-op por construcción. |
| e | `setups/vwap_reclaim.py` | `was_below` es casi un no-op: la condición `c_prev ≤ v_prev` ya implica que la ventana contiene una barra por debajo, salvo igualdad exacta. |
| f | `features/market_structure/native.py` | `order_blocks` busca la vela opuesta empezando **en** la barra de ruptura (`range(b, -1, -1)`), por lo que la propia barra de ruptura puede ser el order block. |
| g | rendimiento | `SetupEngine.detect` recalcula el análisis completo de los 3 timeframes en cada ciclo, y `api/chart.py` lo vuelve a recalcular para el dashboard. Duplicación en cada poll. |

---

## 4. Lo que está bien hecho

No todo son problemas. Estas partes resisten la auditoría:

- **No-lookahead real, no declarado.** `_slice_asof` en un único punto de entrada; todos los
  indicadores son causales (`ewm(adjust=False)`, `min_periods`, `rolling` que termina en la barra
  actual). Los swings requieren `n_right` barras de confirmación y excluyen los no confirmados. El
  ORB se niega explícitamente a disparar antes de que el rango esté completo. Hay tests dedicados
  (`test_resample_no_lookahead.py`, `test_storage_reproducible.py`).
- **Determinismo.** El mismo motor sirve para backtest y para live — sin ramas `if backtesting`.
  Ningún componente del path de decisión lee el reloj de pared ni la red.
- **Separación de capas limpia.** `signals/engine.py` declara y respeta que no puede importar
  `output`. GHOST consume, nunca recomputa. El dashboard es estrictamente read-only.
- **Trazabilidad.** Cada decisión (APPROVED **y** NO_TRADE) se persiste con su desglose de
  confluencia completo. El mismo `signal_id` recorre todo el pipeline. Es auditable de verdad —
  esta auditoría solo fue posible por eso.
- **Honestidad sobre los datos.** `is_delayed = True` siempre; specs CME marcadas como no
  verificadas y el dimensionamiento en dólares deshabilitado en consecuencia.

---

## 5. Prioridad sugerida

| Prioridad | Hallazgo | Impacto | Dónde |
|---|---|---|---|
| 1 | **H-2** target de liquidez opuesta | +58 candidatos (21 % del setup dominante) | config, 1 línea |
| 2 | **H-1** ORB muerto | recupera un setup entero (actualmente 0 %) | `orb.py` o config |
| 3 | **H-3** `min_stop_atr_mult` | 62 bloqueos, umbral en la mediana | config |
| 4 | **H-4** 16 pts constantes | el umbral no significa lo que dice | config + `confluence.py` |
| 5 | **H-5** reversión vs scoring de tendencia | 138 penalizaciones que castigan el diseño | `confluence.py` |
| 6 | **H-9** log sin desglose de confluencia | el audit trail no puede explicar sus rechazos | `engine.py`, 1 línea aditiva |
| 7 | **H-6** sweep sin caducidad | +15 pts espurios | `context.py` |
| 8 | **H-7** zona muerta de EMA | ruido en el sesgo | `context.py` |

Los tres primeros son **config-only o casi**, y juntos deberían mover la tasa de conversión de
forma notable sin tocar la lógica protegida.

> ⚠️ Ninguno de estos cambios está aplicado. Cambiar cualquiera **altera el comportamiento de
> trading**, así que la decisión es tuya. Recomiendo aplicarlos de uno en uno y comparar la tasa de
> conversión con el panel **Embudo** del dashboard, que ahora muestra exactamente estas métricas en
> vivo.

---

## 6. Lo que la auditoría NO cubre

Honestidad sobre los límites de este documento:

- **Rentabilidad.** Esto audita si el motor hace lo que dice, no si gana dinero. El score de
  confluencia es una medida de calidad de condiciones, no una probabilidad de acierto — el propio
  código lo declara, y es correcto.
- **Muestra única.** Todos los números empíricos vienen de un histórico de ES de ~3 días. Las
  proporciones son indicativas; las conclusiones **estructurales** (H-1, H-2) son matemáticas y no
  dependen de la muestra.
- **Specs CME sin verificar.** Todo el análisis es en puntos. El dimensionamiento en dólares sigue
  correctamente deshabilitado.
- **Los datos de Yahoo.** No se auditó la calidad del feed (huecos, revisiones, discrepancias con
  CME), solo cómo lo consume el motor.

---

## 7. Reproducir la auditoría

Los dos scripts usados viven fuera del repo (son herramientas de diagnóstico, no código de
producción). Reconstruirlos:

```python
# Recorre el histórico de muestra por el pipeline REAL y cuenta dónde se pierde cada candidato.
from signal_engine.config.schema import EngineConfig
from signal_engine.data.engine import DataEngine
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine
from signal_engine.ghost.brain import GhostBrain

cfg = EngineConfig.load()
de = DataEngine(cfg)
loaded = de.load_contract(CsvProvider("tests/data_samples"), "ES", "ESU26")
tf_bars = de.build_timeframes(loaded)

setup_engine = SetupEngine(cfg, ContractRegistry.load())
signal_engine = SignalEngine(cfg.signals)
ghost = GhostBrain(cfg.ghost)

for N in tf_bars["5m"].index[60:]:
    ctx = de.analysis_context("ES", N, tf_bars)
    cands = setup_engine.detect(ctx)
    if not cands:
        continue
    results = signal_engine.evaluate_candidates(ctx, cands)
    analysis = setup_engine.analyzer.analyze(ctx)
    ms, decisions = ghost.evaluate(ctx, analysis, results)
    # contar por setup / razón / score de confluencia
```

El desglose de `stop/ATR` por setup sale de `ghost.brain._stop_context(signal, exec_ta, cfg.ghost)`,
y la frecuencia de factores de `decision.confluence.contributions`.
