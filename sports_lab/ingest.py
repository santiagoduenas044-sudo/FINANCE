"""Download nflverse schedules/lines and build the team-perspective table."""
from __future__ import annotations

import csv
import io
import sqlite3
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone

NFLVERSE_GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

GAME_COLS = [
    "game_id", "season", "game_type", "week", "gameday", "weekday", "gametime",
    "away_team", "home_team", "away_score", "home_score", "location",
    "spread_line", "total_line", "away_moneyline", "home_moneyline",
    "away_spread_odds", "home_spread_odds", "under_odds", "over_odds",
    "away_rest", "home_rest", "div_game", "roof", "surface", "temp", "wind",
    "stadium_id", "stadium", "away_qb_name", "home_qb_name",
    "away_coach", "home_coach", "referee",
]
INT_COLS = {"season", "week", "away_score", "home_score", "away_moneyline", "home_moneyline",
            "away_spread_odds", "home_spread_odds", "under_odds", "over_odds",
            "away_rest", "home_rest", "div_game"}
REAL_COLS = {"spread_line", "total_line", "temp", "wind"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_games_csv(url: str = NFLVERSE_GAMES_URL, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "sports-lab/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _conv(col: str, val: str):
    if val is None or val == "" or val == "NA":
        return None
    if col in INT_COLS:
        return int(float(val))
    if col in REAL_COLS:
        return float(val)
    return val


def load_games(conn: sqlite3.Connection, csv_text: str) -> int:
    """Upsert every game from the nflverse CSV. Returns row count."""
    rows = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        rows.append(tuple(_conv(c, r.get(c)) for c in GAME_COLS))
    placeholders = ",".join("?" * len(GAME_COLS))
    conn.executemany(
        f"INSERT OR REPLACE INTO games ({','.join(GAME_COLS)}) VALUES ({placeholders})", rows
    )
    conn.commit()
    return len(rows)


def snapshot_lines(conn: sqlite3.Connection, captured_at: str | None = None) -> int:
    """Store current lines of games not played yet, so line movement builds up over time."""
    captured_at = captured_at or now_iso()
    cur = conn.execute(
        """SELECT g.game_id, g.spread_line, g.total_line, g.home_moneyline, g.away_moneyline
           FROM games g
           WHERE g.home_score IS NULL AND (g.spread_line IS NOT NULL OR g.total_line IS NOT NULL)"""
    )
    n = 0
    for r in cur.fetchall():
        last = conn.execute(
            "SELECT spread_line, total_line, home_moneyline, away_moneyline FROM line_snapshots "
            "WHERE game_id=? ORDER BY captured_at DESC LIMIT 1", (r["game_id"],)
        ).fetchone()
        current = (r["spread_line"], r["total_line"], r["home_moneyline"], r["away_moneyline"])
        if last is not None and tuple(last) == current:
            continue  # unchanged, no need for a new snapshot
        conn.execute("INSERT OR REPLACE INTO line_snapshots VALUES (?,?,?,?,?,?)",
                     (r["game_id"], captured_at, *current))
        n += 1
    conn.commit()
    return n


def _latest_splits(conn):
    out = {}
    for r in conn.execute("SELECT * FROM splits ORDER BY captured_at"):
        out[r["game_id"]] = r  # later captures overwrite earlier ones
    return out


def _opening_lines(conn):
    out = {}
    for r in conn.execute("SELECT * FROM line_snapshots ORDER BY captured_at DESC"):
        out[r["game_id"]] = r  # earliest capture wins
    return out


def _latest_forecasts(conn):
    out = {}
    for r in conn.execute("SELECT * FROM weather_forecasts ORDER BY captured_at"):
        out[r["game_id"]] = r
    return out


def _sign(x):
    return None if x is None else (1 if x > 0 else (0 if x < 0 else None))


def build_team_games(conn: sqlite3.Connection) -> int:
    """Rebuild the one-row-per-team-per-game table with derived and situational fields."""
    games = conn.execute(
        "SELECT * FROM games ORDER BY sport, season, gameday, gametime, game_id"
    ).fetchall()
    splits = _latest_splits(conn)
    opens = _opening_lines(conn)
    fcst = _latest_forecasts(conn)

    rows = []
    for g in games:
        played = g["home_score"] is not None and g["away_score"] is not None
        for side in ("home", "away"):
            other = "away" if side == "home" else "home"
            is_home = side == "home"
            sl = g["spread_line"]
            line = None if sl is None else (-sl if is_home else sl)
            pts = g[f"{side}_score"]
            opp_pts = g[f"{other}_score"]
            margin = pts - opp_pts if played else None
            total = pts + opp_pts if played else None
            ats_margin = margin + line if (played and line is not None) else None
            ou_margin = total - g["total_line"] if (played and g["total_line"] is not None) else None
            covered = _sign(ats_margin)
            over = _sign(ou_margin)
            sp = splits.get(g["game_id"])
            bets = money = None
            if sp is not None:
                if sp["home_bets_pct"] is not None:
                    bets = sp["home_bets_pct"] if is_home else 100 - sp["home_bets_pct"]
                if sp["home_money_pct"] is not None:
                    money = sp["home_money_pct"] if is_home else 100 - sp["home_money_pct"]
            op = opens.get(g["game_id"])
            open_line = open_total = None
            if op is not None:
                if op["spread_line"] is not None:
                    open_line = -op["spread_line"] if is_home else op["spread_line"]
                open_total = op["total_line"]
            fc = fcst.get(g["game_id"])
            hour = None
            if g["gametime"]:
                try:
                    hour = int(g["gametime"].split(":")[0])
                except ValueError:
                    pass
            rows.append({
                "game_id": g["game_id"], "sport": g["sport"] or "nfl", "season": g["season"],
                "month": int(g["gameday"][5:7]) if g["gameday"] else None, "week": g["week"],
                "game_type": g["game_type"], "playoff": 0 if g["game_type"] == "REG" else 1,
                "date": g["gameday"], "weekday": g["weekday"], "hour": hour,
                "team": g[f"{side}_team"], "opp": g[f"{other}_team"],
                "home": 1 if (is_home and g["location"] != "Neutral") else 0,
                "neutral": 1 if g["location"] == "Neutral" else 0,
                "div": g["div_game"],
                "pts": pts, "opp_pts": opp_pts, "margin": margin, "total": total,
                "line": line, "total_line": g["total_line"],
                "ml": g[f"{side}_moneyline"], "opp_ml": g[f"{other}_moneyline"],
                "spread_odds": g[f"{side}_spread_odds"],
                "fav": None if line is None else int(line < 0),
                "dog": None if line is None else int(line > 0),
                "won": int(margin > 0) if played else None,
                "lost": int(margin < 0) if played else None,
                "tie": int(margin == 0) if played else None,
                "covered": covered,
                "ats_push": None if ats_margin is None else int(ats_margin == 0),
                "ats_margin": ats_margin, "ou_margin": ou_margin,
                "over": over, "under": None if over is None else 1 - over,
                "under_odds": g["under_odds"], "over_odds": g["over_odds"],
                "rest": g[f"{side}_rest"], "opp_rest": g[f"{other}_rest"],
                "roof": g["roof"], "surface": g["surface"],
                "outdoors": 1 if g["roof"] in ("outdoors", "open") else 0,
                "temp": g["temp"], "wind": g["wind"],
                "wind_fcst": fc["wind_mph"] if fc is not None else None,
                "coach": g[f"{side}_coach"], "opp_coach": g[f"{other}_coach"],
                "qb": g[f"{side}_qb_name"], "opp_qb": g[f"{other}_qb_name"],
                "bets_pct": bets, "money_pct": money,
                "ou_bets_over_pct": sp["over_bets_pct"] if sp is not None else None,
                "ou_money_over_pct": sp["over_money_pct"] if sp is not None else None,
                "open_line": open_line,
                "line_move": (line - open_line) if (line is not None and open_line is not None) else None,
                "open_total": open_total,
                "total_move": (g["total_line"] - open_total)
                if (g["total_line"] is not None and open_total is not None) else None,
                "rank": g[f"{side}_rank"], "opp_rank": g[f"{other}_rank"],
                "team_name": g[f"{side}_name"], "opp_name": g[f"{other}_name"],
            })

    _add_situational(rows)

    conn.execute("DELETE FROM team_games")
    cols = list(rows[0].keys()) if rows else []
    if rows:
        conn.executemany(
            f"INSERT INTO team_games ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows],
        )
    conn.commit()
    return len(rows)


def _add_situational(rows: list[dict]) -> None:
    """Season-to-date records, streaks and previous-game fields (only info known before kickoff)."""
    by_team = defaultdict(list)
    for r in rows:
        by_team[(r["sport"], r["team"], r["season"])].append(r)  # PIT (NFL) != PIT (NHL)

    prev_of = {}  # (game_id, team) -> previous game row of that team
    last_date = {}  # (sport, team) -> date of the previous game, across seasons, for rest days
    for (sport, team, _season), games in sorted(by_team.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        wins = losses = aw = al = 0
        streak = ats_streak = 0
        prev = None
        for i, r in enumerate(games):
            r.update(game_no=i + 1, wins=wins, losses=losses, ats_wins=aw, ats_losses=al,
                     streak=streak, ats_streak=ats_streak)
            if r["rest"] is None and r["date"] and (sport, team) in last_date:
                r["rest"] = (date.fromisoformat(r["date"]) - date.fromisoformat(last_date[(sport, team)])).days
            if r["date"]:
                last_date[(sport, team)] = r["date"]
            for k in ("won", "lost", "covered", "margin", "pts", "opp_pts", "line", "home",
                      "over", "ou_margin", "ats_margin"):
                r["p_" + k] = prev[k] if prev is not None else None
            prev_of[(r["game_id"], r["team"])] = prev
            if r["won"] is None:
                continue  # not played yet: nothing to accumulate
            if r["won"]:
                wins += 1
                streak = streak + 1 if streak > 0 else 1
            elif r["lost"]:
                losses += 1
                streak = streak - 1 if streak < 0 else -1
            else:
                streak = 0
            if r["covered"] == 1:
                aw += 1
                ats_streak = ats_streak + 1 if ats_streak > 0 else 1
            elif r["covered"] == 0:
                al += 1
                ats_streak = ats_streak - 1 if ats_streak < 0 else -1
            prev = r

    by_key = {(r["game_id"], r["team"]): r for r in rows}
    for r in rows:
        if r["opp_rest"] is None:
            other = by_key.get((r["game_id"], r["opp"]))
            r["opp_rest"] = other["rest"] if other else None
        op = prev_of.get((r["game_id"], r["opp"]))
        r["op_won"] = op["won"] if op else None
        r["op_lost"] = op["lost"] if op else None
        r["op_covered"] = op["covered"] if op else None
        r["op_margin"] = op["margin"] if op else None
        r["op_line"] = op["line"] if op else None


def update(conn: sqlite3.Connection, csv_text: str | None = None) -> dict:
    """Full refresh: download (unless csv_text given), snapshot lines, rebuild derived table."""
    csv_text = csv_text if csv_text is not None else fetch_games_csv()
    n_games = load_games(conn, csv_text)
    n_snap = snapshot_lines(conn)
    n_rows = build_team_games(conn)
    return {"games": n_games, "line_snapshots": n_snap, "team_rows": n_rows}
