# Sports Lab — your own BetLabs + KillerSports (NFL, CFB, NBA, CBB, NHL, MLB)

Independent from the futures bot: it imports nothing from `signal_engine` and only uses
the Python 3.10+ standard library.

| Part | What it does |
|---|---|
| **Database** | NFL since 1999 (nflverse, free): closing spread, total and moneyline, prices, weather, rest, QB, coach, roof and surface. CFB, NBA, CBB, NHL and MLB come from ESPN (`download`). |
| **Query** | SDQL / KillerSports syntax (`team = PIT and p:ATSW and H`). SU / ATS / O-U with ROI and profit per $100, average margins, teasers ±6/±10, by-season breakdown, out-of-sample check and a full game log. |
| **Discover** | Trend packs, your own systems library, a swipeable board of upcoming matches and a self-grading portfolio. |
| **Projections** (NFL) | Power ratings → PRO line, total, fair moneyline and an A–F grade per bet, like Action's PRO tab. |
| **Weather** (NFL) | Kickoff wind, gusts, temperature and rain forecast (Open-Meteo, free, no API key). |
| **Splits** | Log the % tickets / % money you see in Action, DK or VSiN and build your own history. |
| **Line movement** | Every `update` snapshots current lines, so you get openers and moves over time. |

## Setup (Mac)

```bash
cd ~/FINANCE
python3 -m sports_lab update      # downloads ~7,500 NFL games (a few seconds)
python3 -m sports_lab train       # calibrates the model + backtest (~10 s)
python3 -m sports_lab web         # opens http://localhost:8765
```

To use it from your phone or iPad on the same Wi-Fi: `python3 -m sports_lab web --host 0.0.0.0`,
then open `http://<your-Mac-IP>:8765`.

Data lives in `sports_lab/data/` (git-ignored). Run `update` on game days: it refreshes results,
new lines (movement), weather, the ESPN sports you downloaded, and grades your portfolio.

## Other sports (ESPN)

```bash
python3 -m sports_lab download cfb --from 2018     # College Football FBS
python3 -m sports_lab download nba --from 2018
python3 -m sports_lab download cbb --from 2020     # College Basketball D-I (lots of games)
python3 -m sports_lab download nhl --from 2018
python3 -m sports_lab download mlb --from 2018
```

- One request per day to ESPN's public scoreboard (no key). A season takes 1–2 minutes.
- Stores the score, AP rank (college) and ESPN's line; recent seasons include open and close, and the
  **closing** number is used. Older seasons may miss lines for some games.
- `--from 2018` is the season that **starts** in 2018. ESPN labels NBA, NHL and CBB with the year the
  season ends, so 2025-26 shows up as 2026.
- This part could not be tested against live ESPN where it was written (network blocked); tests use
  responses in ESPN's format. If something looks off, share the error.

## Query language

Each row is **one team in one game** (like SDQL). Pick the sport in the web app or pass `--sport`:

```bash
python3 -m sports_lab query "total < 40 and wind >= 12 and outdoors"
python3 -m sports_lab query "team = PIT and p:ATSW and H and season > 2020"
python3 -m sports_lab query "A and D and p:margin <= -14"
python3 -m sports_lab query "F and rest > o:rest + 3" --from 2010
python3 -m sports_lab query "H and rank <= 10 and line <= -21" --sport cfb
python3 -m sports_lab query "money_pct >= 60 and bets_pct <= 40"      # with your splits
python3 -m sports_lab fields                                           # every field
```

- **Prefixes**: `p:` team's previous game, `pp:` two games ago, `o:` opponent, `op:` opponent's previous game.
  Examples: `p:W`, `p:points >= 30`, `o:rest`, `o:rank <= 10`, `op:L`.
- **Shorthands**: `H` home, `A` away, `N` neutral, `F` favorite, `D` underdog, `W` / `L` won / lost,
  `ATSW` / `ATSL` / `ATSP` cover / no cover / push, `O` / `U` over / under.
- `line` is the team spread (negative = favorite). `total` is the closing over/under, as in SDQL;
  `game_total` is the actual combined score.
- Operators: `= != < <= > >=`, `and / or / not`, `in (A, B)`, `is null`, parentheses and arithmetic.
- **"Interpreted as"** under the query shows how every shorthand was read.
- Buttons: ▶ run, ⤴ copy a link to the query, ☆ save it as a system.
- `wind` and `temp` are the **actual** game conditions; `wind_fcst` is the **forecast** for upcoming games.
- Old field spellings (`p_margin`, `opp_rest`, `total_line`) still work.

### Reading the results

- **ROI** uses real closing prices when available, otherwise -110.
- **p** is the chance of a record at least this good by pure luck if the true rate were 52.4% (break-even at -110).
  Under 0.05 is interesting — but if you tried 50 filters, one will look like that by chance.
- **Out of sample** compares older seasons with recent ones. If a trend only worked in the past, be skeptical.
- If the filter includes both teams of a game (e.g. wind only), ATS is 50% by construction — read Over/Under.
- **Teasers** move the number ±6 or ±10: +6 ATS = the team gets 6 more points; +6 on totals = the total is 6 points higher.

## Discover: systems, packs, board and portfolio

- **System** = a query + what to bet when a game matches: team ATS or moneyline, or game over/under.
  Create one with "+ New system" or ☆ from Query. Records recompute live.
- **Trend Packs**: starter bundles (NFL Weather, NFL Situational, CFB Rankings, NBA Rest, NHL Bounce-back).
  "Add pack" copies them into your library. CFB/NBA/NHL need `download` for that sport first.
- **Proven** = 40+ games, p ≤ 0.15, positive ROI and profitable both before and after the out-of-sample split.
  Not a guarantee — just a filter against the most likely flukes.
- **Swipe the Board**: upcoming games (next 7 days) that match your systems. Swipe right (✓) to tail, left (✕) to pass.
  Filters for sport, ranking, "Proven only" and "Live today".
- **Portfolio**: each tailed pick locks the line and price at that moment. When the game is final and you run
  `update`, it grades itself (W/L/P, 1 unit per pick). Totals, per-system results and every pick.
- Everything is stored in your local database. No accounts, no community: it's your personal desk.

## Projections (PRO model)

```bash
python3 -m sports_lab projections
python3 -m sports_lab projections --season 2026 --week 5
python3 -m sports_lab ratings
```

How it works, never using future information:

1. **Results ratings**: ridge regression of game margins (capped at 24) with exponential decay (~4-month half-life).
   Early in a season it shrinks toward average automatically.
2. **Market ratings**: the same regression on past closing lines (what the market already knows about injuries, QB changes…).
3. **Raw model** = a blend of both, weight chosen on training seasons.
4. **PRO line** = market line + *k* × (raw model − market). *k* is estimated from history: if the model knows
   nothing beyond the closing line, *k* comes out near 0 and the model says so.
5. **Totals**: same idea plus a **wind** adjustment for how much the market under-reacts (currently about
   −0.25 points per mph above 8).
6. **Probabilities**: a margin curve that respects key numbers (3, 7, 10…) centred on the PRO line; fair ML is
   anchored to the no-vig market price.
7. **Grade** = expected value at the posted price: A ≥ +8%, B ≥ +3%, C ≥ 0%. D is roughly a coin flip paying the juice; F is worse.

The **Model** tab (and `train`) shows the walk-forward backtest by grade. Current result (trained 2000–2015,
tested 2016–2026): the model does **not** beat the closing line on spreads (error 12.73 vs 12.72). Totals show a
slight edge tied to wind. Use it to find disagreements and confirm them with trends, weather and splits.

## Weather

```bash
python3 -m sports_lab weather
```

Hourly stadium forecast averaged over the 3 game hours; domes and closed roofs are skipped; 12+ mph is flagged.
The totals model uses the forecast.

## Splits (% tickets vs % money)

Nobody gives away split history for free — here you build it. Log them in the **Splits** tab or:

```bash
python3 -m sports_lab splits-add CIN@PIT --home-bets 38 --home-money 55 --over-bets 60 --over-money 45 --source Action
python3 -m sports_lab splits-import my_splits.csv
```

CSV format (see `splits_example.csv`): `game_id,season,week,source,home_bets_pct,home_money_pct,over_bets_pct,over_money_pct`.
`game_id` can be `2026_03_CIN_PIT` or `CIN@PIT` (with `season` and `week`).

## Tests

```bash
python3 -m pip install pytest
python3 -m pytest sports_lab/tests
```

## Design

Graphite and champagne-gold night theme, warm ivory day theme (Auto / Day / Night toggle, top right).
Fonts — Big Shoulders Display, Barlow and IBM Plex Mono — are SIL Open Font License and bundled in
`static/fonts/`, so the app works offline. The original Spanish CLI command names still work.
