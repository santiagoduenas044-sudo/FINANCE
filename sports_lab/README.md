# Sports Lab — tu BetLabs + KillerSports para la NFL

Es independiente del bot de futuros: no importa nada de `signal_engine` y no usa
ninguna dependencia fuera de la librería estándar de Python 3.10+.

| Parte | Qué hace |
|---|---|
| **Base de datos** | Todos los partidos NFL desde 1999 (nflverse, gratis), con spread, total y moneyline de cierre, momios, clima, descanso, QB, coach, techo y superficie. |
| **Buscador de tendencias** | Lenguaje tipo SDQL. Da récord SU / ATS / ML / Over-Under, ROI, valor p, desglose por temporada y **prueba fuera de muestra**. |
| **Proyecciones** | Power ratings + línea PRO, total, ML justo y grade (A-F) por partido, como la pestaña PRO de Action. |
| **Clima** | Pronóstico de viento, ráfagas, temperatura y lluvia a la hora del partido (Open-Meteo, gratis, sin API key). |
| **Splits** | Guarda el % de tickets y de dinero que veas en Action, DK o VSiN, y así armas tu propio historial. |
| **Movimiento de línea** | Cada vez que corres `actualizar` se guarda la línea actual, así sabes la apertura y cuánto se movió. |

## Instalación (Mac)

```bash
cd ~/FINANCE
python3 -m sports_lab actualizar      # descarga ~7,500 partidos (unos segundos)
python3 -m sports_lab entrenar        # calibra el modelo + backtest (~10 s)
python3 -m sports_lab web             # abre http://localhost:8765
```

Para verlo desde el celular en la misma red WiFi: `python3 -m sports_lab web --host 0.0.0.0`,
luego abre `http://<IP-de-tu-Mac>:8765`.

Los datos quedan en `sports_lab/data/` (ignorada por git). Corre `actualizar` cada día de partido:
así se actualizan los resultados, las líneas nuevas (movimiento) y el clima.

## Buscador de tendencias

Cada fila es **un equipo en un partido** (como SDQL). Ejemplos:

```bash
python3 -m sports_lab buscar "total_line < 40 and wind >= 12 and outdoors"
python3 -m sports_lab buscar "home and dog and p_margin <= -14"
python3 -m sports_lab buscar "team = PIT and line > 0 and rest > opp_rest" --desde 2010
python3 -m sports_lab buscar "div and temp <= 32 and roof = outdoors"
python3 -m sports_lab buscar "money_pct >= 60 and bets_pct <= 40"      # con tus splits
python3 -m sports_lab campos                                            # todos los campos
```

- Operadores: `= != < <= > >=`, `and / or / not` (también `y / o / no`), `in (A, B)`, `is null`, paréntesis y sumas (`rest > opp_rest + 3`).
- Un campo solo equivale a `= 1`: `home`, `dog`, `p_lost`, `outdoors`.
- `line` es el spread del equipo: negativo = favorito, como en las casas de apuestas.
- `p_*` es el partido anterior del equipo. `op_*` es el partido anterior del rival.
- `wind` y `temp` son el clima **real** del partido. `wind_fcst` es el **pronóstico** para partidos por jugar.

### Cómo leer el resultado

- **ROI** usa los momios reales de cierre cuando existen; si no, usa -110.
- **p** es la probabilidad de lograr ese récord (o uno mejor) por pura suerte si en realidad fuera 52.4%, el punto de equilibrio a -110. Menos de 0.05 es interesante. Pero si probaste 50 filtros, alguno va a salir así por azar.
- **Fuera de muestra**: compara las temporadas viejas con las recientes. Si una tendencia solo funcionó antes, desconfía.
- Si el filtro incluye a los dos equipos de un partido (por ejemplo, solo viento), el ATS sale 50% por construcción. Para esos filtros mira Over/Under.

## Proyecciones (modelo PRO)

```bash
python3 -m sports_lab proyecciones                        # próxima semana
python3 -m sports_lab proyecciones --temporada 2026 --semana 4
python3 -m sports_lab ratings
```

Cómo funciona, sin ver el futuro en ningún paso:

1. **Ratings por resultados**: regresión ridge de los márgenes de victoria (topados en 24) con decaimiento
   exponencial (vida media de unos 4 meses). Con pocos datos, al inicio de temporada, se encoge solo hacia el promedio.
2. **Ratings de mercado**: la misma regresión, pero sobre las líneas de cierre anteriores (lo que el mercado
   ya sabe de lesiones, cambios de QB, etc.).
3. **Modelo puro** = mezcla de los dos. El peso se elige en las temporadas de entrenamiento.
4. **Línea PRO** = línea del mercado + *k* × (modelo puro − mercado). *k* se estima con la historia: si el
   modelo no sabe más que la línea de cierre, *k* sale cerca de 0 y el modelo te lo dice honestamente.
5. **Totales**: igual, más un ajuste por **viento** que mide cuánto se queda corto el mercado en partidos
   con viento (hoy, unos -0.25 puntos por cada mph arriba de 8).
6. **Probabilidades**: una curva de márgenes que respeta los números clave (3, 7, 10...) y está centrada en la línea PRO.
   El ML justo se ancla al precio del mercado sin vig.
7. **Grade** = valor esperado al momio ofrecido: A ≥ +8%, B ≥ +3%, C ≥ 0%. D es más o menos una apuesta
   pareja pagando el juice. F es peor.

La pestaña **Modelo** de la web (y `entrenar`) muestra el backtest walk-forward por grade.
Resultado actual (entrenado en 2000-2015, probado en 2016-2026): el modelo **no** le gana a la línea de cierre en
spreads (error 12.73 vs 12.72). En totales hay una ventaja leve, ligada al viento. Úsalo para encontrar desacuerdos
y confírmalos con tendencias, clima y splits. No tomes las grades como dinero garantizado.

## Clima

```bash
python3 -m sports_lab clima
```

Trae el pronóstico por hora del estadio y promedia las 3 horas del partido. Omite domos y techos cerrados.
Marca con "viento fuerte" los partidos con 12+ mph. El modelo de totales usa ese pronóstico.

## Splits (% tickets vs % dinero)

Nadie regala el historial de splits. Aquí lo construyes tú: captúralos desde la pestaña **Splits** de la web o así:

```bash
python3 -m sports_lab splits-agregar CIN@PIT --home-bets 38 --home-money 55 --over-bets 60 --over-money 45 --fuente Action
python3 -m sports_lab splits-importar mis_splits.csv
```

Formato del CSV (ver `splits_ejemplo.csv`): `game_id,season,week,source,home_bets_pct,home_money_pct,over_bets_pct,over_money_pct`.
`game_id` puede ser `2026_03_CIN_PIT` o `CIN@PIT` (con `season` y `week`).

## Pruebas

```bash
python3 -m pip install pytest
python3 -m pytest sports_lab/tests
```

## Diseño

La web usa tres fuentes: Big Shoulders Display (títulos), Barlow (texto) e IBM Plex Mono (números).
Las tres tienen licencia SIL Open Font License y vienen incluidas en `static/fonts/`, así que funcionan sin internet.
El botón de arriba a la derecha cambia entre tema automático, día (papel) y noche.
