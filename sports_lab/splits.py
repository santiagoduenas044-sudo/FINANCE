"""Public betting splits (% tickets / % money) that you capture yourself.

There is no free source with *history*, so the lab stores every capture you add
(from the Action app, DK Network, VSiN...). After a season you own the history.

CSV columns (header required; percentages 0-100, empty allowed):
    game_id,captured_at,source,home_bets_pct,home_money_pct,over_bets_pct,over_money_pct
`game_id` may also be written as AWAY@HOME plus a `season` and `week` column,
e.g. `CIN@PIT,2026,3`.
"""
from __future__ import annotations

import csv
import io
import sqlite3

from .ingest import build_team_games, now_iso

PCT_COLS = ("home_bets_pct", "home_money_pct", "over_bets_pct", "over_money_pct")


def resolve_game_id(conn: sqlite3.Connection, game: str, season: int | None = None,
                    week: int | None = None, sport: str = "nfl") -> str:
    game = game.strip()
    if conn.execute("SELECT 1 FROM games WHERE game_id=?", (game,)).fetchone():
        return game
    if "@" in game:
        away, home = (t.strip().upper() for t in game.split("@", 1))
        sql = "SELECT game_id FROM games WHERE away_team=? AND home_team=? AND sport=?"
        params: list = [away, home, sport]
        if season:
            sql += " AND season=?"
            params.append(season)
        if week:
            sql += " AND week=?"
            params.append(week)
        else:
            sql += " AND home_score IS NULL"  # default: the next scheduled meeting
        row = conn.execute(sql + " ORDER BY gameday LIMIT 1", params).fetchone()
        if row:
            return row["game_id"]
    raise ValueError(f"Game not found: {game!r} (use 2026_03_CIN_PIT or CIN@PIT)")


def _pct(v):
    if v is None or str(v).strip() == "":
        return None
    x = float(str(v).strip().rstrip("%"))
    if not 0 <= x <= 100:
        raise ValueError(f"Percentage out of range: {v}")
    return x


def add(conn: sqlite3.Connection, game: str, source: str = "manual", captured_at: str | None = None,
        season: int | None = None, week: int | None = None, rebuild: bool = True, **pcts) -> str:
    game_id = resolve_game_id(conn, game, season, week)
    values = [_pct(pcts.get(c)) for c in PCT_COLS]
    if all(v is None for v in values):
        raise ValueError("Add at least one percentage")
    conn.execute("INSERT OR REPLACE INTO splits VALUES (?,?,?,?,?,?,?)",
                 (game_id, captured_at or now_iso(), source, *values))
    conn.commit()
    if rebuild:
        build_team_games(conn)
    return game_id


def import_csv(conn: sqlite3.Connection, text: str, source: str = "csv") -> int:
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        add(conn, row["game_id"], source=row.get("source") or source,
            captured_at=row.get("captured_at") or None,
            season=int(row["season"]) if row.get("season") else None,
            week=int(row["week"]) if row.get("week") else None,
            rebuild=False, **{c: row.get(c) for c in PCT_COLS})
        n += 1
    build_team_games(conn)
    return n


def latest(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT s.*, g.away_team, g.home_team, g.gameday FROM splits s
           JOIN games g USING (game_id) ORDER BY s.captured_at DESC LIMIT ?""", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
