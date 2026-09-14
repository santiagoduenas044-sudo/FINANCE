# PHASE3_DATA_ENGINE.md — Implementación del Data Engine

> Estado de la Fase 3. Infraestructura de datos **únicamente** (sin estrategia,
> setups, signal engine, auto-trading, TradingView ni IA).
> Diseño de referencia: [`MARKET_DATA.md`](./MARKET_DATA.md) · [`ARCHITECTURE.md`](./ARCHITECTURE.md).

## ⛔ Bloqueo de verificación CME (condición #1)

`cmegroup.com` está **bloqueado por el proxy de egress** de este entorno, por lo
que **no fue posible verificar los `ContractSpec` contra la documentación oficial
de CME Group**. Conforme a la condición #1, los specs se cargan con
**`verified: false`** y valores marcados como PROVISIONALES en
`signal_engine/config/contracts.yaml`. `ContractRegistry.require_verified()`
**lanza excepción** para impedir que un consumidor (p.ej. el futuro RiskEngine)
opere con specs no verificados. Detalle y vías de resolución en
[`CME_VERIFICATION.md`](./CME_VERIFICATION.md).

## Pipeline implementado

```
CSV/FirstRate → raw ingestion → normalización (tz/DST, close-labeling)
   → validación (duplicados, orden, gaps, OHLCV)
   → identificación de contrato (código, mes, vencimiento)
   → sesiones (RTH/ETH, trade-date)
   → resample multi-TF (5m/15m/1H) sin look-ahead, por sesión
   → rollover (volumen) + serie continua (raw / ratio)
   → almacenamiento Parquet (+ hash reproducible)
   → AnalysisContext (vista as-of N)
```

## Cómo ejecutar

```bash
pip install -r requirements.txt
python scripts/generate_sample_data.py          # genera CSVs de ejemplo
PYTHONPATH=. python scripts/demo_pipeline.py     # corre el pipeline end-to-end
python -m pytest -q                              # 48 tests
```

## Decisiones clave

- **Todas las barras se normalizan a etiquetado por CIERRE.** La sesión se
  asigna por el **instante de negociación (apertura)** de la barra, no por su
  etiqueta de cierre (así la barra 09:29→09:30 pre-apertura no cuenta como RTH).
- **Resample por grupo `(trading_date, sesión)`** con origen en la apertura de
  sesión y bins `label='right', closed='right'` → una barra HTF sólo es visible
  cuando su cierre `<= decision_time` (no-lookahead por construcción).
- **`MarketDataProvider` como única dependencia del núcleo** → CSV/FirstRate hoy,
  Databento después sin tocar el núcleo (test lo demuestra).
- **Rollover por volumen, reproducible**; serie continua `raw` (niveles reales) y
  `ratio` (estadística sin saltos).
- **Reproducibilidad** verificada por hash determinista del DataFrame (no depende
  de mtime del archivo).

## Trade-offs

- Las barras HTF llevan etiqueta = cierre del bin; la última barra parcial de una
  sesión se vuelve visible en el borde del bin (conservador, nunca antes de
  tiempo).
- El resample de ETH se alinea a la apertura ETH; el hueco de RTH produce bins
  vacíos (descartados).
- El chequeo de gaps requiere misma sesión **y** misma trade-date, para no marcar
  la pausa de mantenimiento como anomalía.

## Problemas encontrados con los datos

1. **CME inaccesible** (egress) → specs no verificados (ver arriba).
2. **Convención de timestamp del origen**: FirstRateData etiqueta al inicio de la
   barra → se normaliza a cierre (configurable `input_timestamp_convention`).
3. **Sesión ETH no contigua** (overnight + post-RTH dentro del mismo trade-date)
   → tratada correctamente agrupando por `(trading_date, sesión)`.
