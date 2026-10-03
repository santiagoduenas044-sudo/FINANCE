# Sports Lab — tu BetLabs + KillerSports (NFL, CFB, NBA, CBB, NHL, MLB)

Es independiente del bot de futuros: no importa nada de `signal_engine` y no usa
ninguna dependencia fuera de la librería estándar de Python 3.10+.

| Parte | Qué hace |
|---|---|
| **Base de datos** | NFL desde 1999 (nflverse, gratis), con spread, total y moneyline de cierre, momios, clima, descanso, QB, coach, techo y superficie. CFB, NBA, CBB, NHL y MLB se bajan de ESPN (`descargar`). |
| **Buscador de tendencias** | Sintaxis tipo SDQL / KillerSports (`team = PIT and p:ATSW and H`). Da SU / ATS / O-U con ROI y ganancia por $100, márgenes promedio, teasers ±6/±10, desglose por temporada, prueba fuera de muestra y el registro de cada partido. |
| **Proyecciones** (NFL) | Power ratings + línea PRO, total, ML justo y grade (A-F) por partido, como la pestaña PRO de Action. |
| **Clima** (NFL) | Pronóstico de viento, ráfagas, temperatura y lluvia a la hora del partido (Open-Meteo, gratis, sin API key). |
| **Splits** | Guarda el % de tickets y de dinero que veas en Action, DK o VSiN, y así armas tu propio historial. |
| **Movimiento de línea** | Cada vez que corres `actualizar` se guarda la línea actual, así sabes la apertura y cuánto se movió. |

## Instalación (Mac)

```bash
cd ~/FINANCE
python3 -m sports_lab actualizar      # descarga ~7,500 partidos NFL (unos segundos)
python3 -m sports_lab entrenar        # calibra el modelo + backtest (~10 s)
python3 -m sports_lab web             # abre http://localhost:8765
```

Para verlo desde el celular o iPad en la misma red WiFi: `python3 -m sports_lab web --host 0.0.0.0`,
luego abre `http://<IP-de-tu-Mac>:8765`.

Los datos quedan en `sports_lab/data/` (ignorada por git). Corre `actualizar` cada día de partido:
así se actualizan los resultados, las líneas nuevas (movimiento), el clima y los deportes de ESPN que ya bajaste.

## Otros deportes (ESPN)

```bash
python3 -m sports_lab descargar cfb --desde 2018     # College Football FBS
python3 -m sports_lab descargar nba --desde 2018
python3 -m sports_lab descargar cbb --desde 2020     # College Basketball D-I (muchos partidos)
python3 -m sports_lab descargar nhl --desde 2018
python3 -m sports_lab descargar mlb --desde 2018
```

- Pide un día a la vez al scoreboard público de ESPN (sin API key). Una temporada tarda 1-2 minutos.
- Guarda el marcador, el ranking AP (college) y la línea que muestra ESPN. En temporadas recientes ESPN trae
  apertura y cierre, y se usa el **cierre**. En temporadas viejas puede faltar la línea de algunos partidos.
- `--desde 2018` es la temporada que **empieza** en 2018. ESPN etiqueta NBA, NHL y CBB con el año en que
  termina, así que la 2025-26 aparece como 2026.
- Esta parte no se pudo probar contra ESPN real desde donde se escribió (red bloqueada); los tests usan
  respuestas con el formato de ESPN. Si algo sale raro, avísame con el error.

## Buscador de tendencias

Cada fila es **un equipo en un partido** (como SDQL). En la web eliges el deporte arriba; en la terminal usas `--deporte`:

```bash
python3 -m sports_lab buscar "total < 40 and wind >= 12 and outdoors"
python3 -m sports_lab buscar "team = PIT and p:ATSW and H and season > 2020"
python3 -m sports_lab buscar "A and D and p:margin <= -14"
python3 -m sports_lab buscar "F and rest > o:rest + 3" --desde 2010
python3 -m sports_lab buscar "H and rank <= 10 and line <= -21" --deporte cfb
python3 -m sports_lab buscar "money_pct >= 60 and bets_pct <= 40"      # con tus splits
python3 -m sports_lab campos                                            # todos los campos
```

- **Prefijos**: `p:` partido anterior del equipo, `pp:` hace dos partidos, `o:` el rival, `op:` el rival en su partido anterior.
  Ejemplos: `p:W`, `p:points >= 30`, `o:rest`, `o:rank <= 10`, `op:L`.
- **Atajos**: `H` local, `A` visitante, `N` neutral, `F` favorito, `D` underdog, `W` / `L` ganó / perdió,
  `ATSW` / `ATSL` / `ATSP` cubrió / no cubrió / push, `O` / `U` over / under.
- `line` es el spread del equipo (negativo = favorito). `total` es el over/under de cierre, como en SDQL;
  `game_total` son los puntos reales.
- Operadores: `= != < <= > >=`, `and / or / not` (también `y`), `in (A, B)`, `is null`, paréntesis y sumas.
- Debajo de la consulta sale **"Interpretado como"**: así ves cómo se entendió cada atajo.
- Botones: ▶ buscar, ↗ copiar link de la consulta, ★ guardarla (se guarda en ese navegador).
- `wind` y `temp` son el clima **real** del partido. `wind_fcst` es el **pronóstico** para partidos por jugar.
- Los nombres viejos (`p_margin`, `opp_rest`, `total_line`) siguen funcionando.

### Cómo leer el resultado

- **ROI** usa los momios reales de cierre cuando existen; si no, usa -110.
- **p** es la probabilidad de lograr ese récord (o uno mejor) por pura suerte si en realidad fuera 52.4%, el punto de equilibrio a -110. Menos de 0.05 es interesante. Pero si probaste 50 filtros, alguno va a salir así por azar.
- **Fuera de muestra**: compara las temporadas viejas con las recientes. Si una tendencia solo funcionó antes, desconfía.
- Si el filtro incluye a los dos equipos de un partido (por ejemplo, solo viento), el ATS sale 50% por construcción. Para esos filtros mira Over/Under.
- **Teasers**: mueven la línea ±6 o ±10 puntos. +6 en ATS = el equipo recibe 6 puntos más; +6 en totales = el total sube 6 puntos (mejor para el Under).

## Descubrir: sistemas, packs, tablero y portafolio

- **Sistema** = una consulta + qué apostar cuando un partido la cumple: ATS o moneyline del equipo, u Over/Under del partido.
  Se crea en Descubrir → "+ Nuevo sistema", o con ★ desde Consulta. Su récord se recalcula en vivo cada vez.
- **Packs**: grupos de sistemas para empezar (NFL Clima, NFL Situacional, CFB, NBA, NHL). "Agregar" los copia a tu biblioteca.
  Los de CFB/NBA/NHL necesitan que primero hagas `descargar` de ese deporte.
- **Probado** = 40+ partidos, p ≤ 0.15, ROI positivo y rentable tanto antes como después del corte fuera de muestra.
  No es garantía: solo filtra lo que más probablemente sea suerte.
- **Tablero**: partidos de los próximos 7 días que cumplen tus sistemas. Desliza a la derecha (o ✓) para seguir la apuesta
  y a la izquierda (o ✕) para pasar. Filtros por deporte, orden, "solo probados" y "solo hoy".
- **Portafolio**: cada apuesta seguida guarda la línea y el momio de ese momento. Cuando el partido termina y corres
  `actualizar`, se califica sola (W/L/P y unidades a 1 unidad por apuesta). Ves el récord total, por sistema y cada apuesta.
- Todo se guarda en tu base de datos local (`sports_lab/data/`). No hay cuentas ni comunidad: es tu escritorio personal.

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
