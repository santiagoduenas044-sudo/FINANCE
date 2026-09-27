"""SQLite storage: schema and connection helper."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("SPORTS_LAB_DATA", Path(__file__).resolve().parent / "data"))
DB_PATH = DATA_DIR / "nfl.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    season INTEGER, game_type TEXT, week INTEGER,
    gameday TEXT, weekday TEXT, gametime TEXT,
    away_team TEXT, home_team TEXT,
    away_score INTEGER, home_score INTEGER,
    location TEXT,
    -- spread_line > 0 means the HOME team is favored (nflverse convention)
    spread_line REAL, total_line REAL,
    away_moneyline INTEGER, home_moneyline INTEGER,
    away_spread_odds INTEGER, home_spread_odds INTEGER,
    under_odds INTEGER, over_odds INTEGER,
    away_rest INTEGER, home_rest INTEGER,
    div_game INTEGER, roof TEXT, surface TEXT, temp REAL, wind REAL,
    stadium_id TEXT, stadium TEXT,
    away_qb_name TEXT, home_qb_name TEXT,
    away_coach TEXT, home_coach TEXT, referee TEXT
);

-- One row per team per game (SDQL-style "team perspective"), rebuilt on ingest.
CREATE TABLE IF NOT EXISTS team_games (
    game_id TEXT, season INTEGER, week INTEGER, game_type TEXT, playoff INTEGER,
    date TEXT, weekday TEXT, hour INTEGER,
    team TEXT, opp TEXT, home INTEGER, neutral INTEGER, div INTEGER,
    pts INTEGER, opp_pts INTEGER, margin INTEGER, total INTEGER,
    line REAL, total_line REAL, ml INTEGER, opp_ml INTEGER, spread_odds INTEGER,
    fav INTEGER, dog INTEGER,
    won INTEGER, lost INTEGER, tie INTEGER, covered INTEGER, ats_push INTEGER,
    ats_margin REAL, ou_margin REAL, over INTEGER, under INTEGER,
    under_odds INTEGER, over_odds INTEGER,
    rest INTEGER, opp_rest INTEGER,
    roof TEXT, surface TEXT, outdoors INTEGER, temp REAL, wind REAL,
    wind_fcst REAL, coach TEXT, opp_coach TEXT, qb TEXT, opp_qb TEXT,
    game_no INTEGER, wins INTEGER, losses INTEGER, ats_wins INTEGER, ats_losses INTEGER,
    streak INTEGER, ats_streak INTEGER,
    p_won INTEGER, p_lost INTEGER, p_covered INTEGER, p_margin INTEGER,
    p_pts INTEGER, p_opp_pts INTEGER, p_line REAL, p_home INTEGER,
    p_over INTEGER, p_ou_margin REAL, p_ats_margin REAL,
    op_won INTEGER, op_lost INTEGER, op_covered INTEGER, op_margin INTEGER, op_line REAL,
    bets_pct REAL, money_pct REAL, ou_bets_over_pct REAL, ou_money_over_pct REAL,
    open_line REAL, line_move REAL, open_total REAL, total_move REAL,
    PRIMARY KEY (game_id, team)
);

-- Public betting splits you capture (CSV import or manual entry).
CREATE TABLE IF NOT EXISTS splits (
    game_id TEXT, captured_at TEXT, source TEXT,
    home_bets_pct REAL, home_money_pct REAL,
    over_bets_pct REAL, over_money_pct REAL,
    PRIMARY KEY (game_id, captured_at, source)
);

-- Line snapshots taken every time you run `actualizar` before kickoff.
CREATE TABLE IF NOT EXISTS line_snapshots (
    game_id TEXT, captured_at TEXT,
    spread_line REAL, total_line REAL, home_moneyline INTEGER, away_moneyline INTEGER,
    PRIMARY KEY (game_id, captured_at)
);

-- Weather forecasts at kickoff for upcoming outdoor games.
CREATE TABLE IF NOT EXISTS weather_forecasts (
    game_id TEXT, captured_at TEXT,
    wind_mph REAL, gust_mph REAL, temp_f REAL, precip_mm REAL,
    PRIMARY KEY (game_id, captured_at)
);

CREATE INDEX IF NOT EXISTS tg_season ON team_games(season, week);
CREATE INDEX IF NOT EXISTS g_season ON games(season, week);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
