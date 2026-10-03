"""Scores and betting lines for CFB, NBA, CBB, NHL and MLB from ESPN's public scoreboard.

ESPN's scoreboard JSON (no key needed) carries final scores plus the odds of its
partner book (spread, total, moneylines; newer seasons also open/close values).
We store the CLOSING numbers when ESPN provides them, otherwise whatever line it
shows. NFL keeps coming from nflverse, which has cleaner closing lines and weather.

Season labels follow ESPN: CFB/MLB use the calendar year the season starts; NBA,
NHL and CBB use the year the season ENDS (2025-26 = 2026).
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # tz database missing: Eastern standard time is close enough for dates
    _ET = timezone(timedelta(hours=-5))

BASE = "https://site.api.espn.com/apis/site/v2/sports"

# sport -> (ESPN path, extra query params, months covered by one season as (start, end) month-day)
SPORTS: dict[str, dict] = {
    "cfb": {"path": "football/college-football", "params": {"groups": "80"}, "start": (8, 20), "end": (1, 20),
            "label": "College Football (FBS)"},
    "nba": {"path": "basketball/nba", "params": {}, "start": (10, 1), "end": (6, 30), "label": "NBA"},
    "cbb": {"path": "basketball/mens-college-basketball", "params": {"groups": "50"}, "start": (11, 1),
            "end": (4, 10), "label": "College Basketball (D-I)"},
    "nhl": {"path": "hockey/nhl", "params": {}, "start": (10, 1), "end": (6, 30), "label": "NHL"},
    "mlb": {"path": "baseball/mlb", "params": {}, "start": (3, 15), "end": (11, 5), "label": "MLB"},
}
OUTDOOR_SPORTS = {"cfb", "mlb"}  # roof/weather only matters here

UPSERT_COLS = [
    "game_id", "sport", "season", "game_type", "week", "gameday", "weekday", "gametime",
    "away_team", "home_team", "away_score", "home_score", "location",
    "spread_line", "total_line", "away_moneyline", "home_moneyline",
    "away_spread_odds", "home_spread_odds", "under_odds", "over_odds",
    "roof", "temp", "stadium", "away_rank", "home_rank", "away_name", "home_name",
]
# never overwrite a known line with "no line" (ESPN sometimes drops odds after the game)
KEEP_IF_NULL = {"spread_line", "total_line", "away_moneyline", "home_moneyline", "away_spread_odds",
                "home_spread_odds", "under_odds", "over_odds", "temp", "away_rank", "home_rank"}


def fetch_day(sport: str, day: date, timeout: int = 30) -> dict:
    cfg = SPORTS[sport]
    params = {"dates": day.strftime("%Y%m%d"), "limit": "1000", **cfg["params"]}
    url = f"{BASE}/{cfg['path']}/scoreboard?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 sports-lab/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------------ parsing helpers
def _num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().lower().lstrip("ou")
    if s in ("", "-", "off", "n/a"):
        return None
    if s in ("even", "ev", "pk", "pick"):
        return 0.0
    try:
        return float(s.replace("+", "").replace("½", ".5"))
    except ValueError:
        return None


def _odds(x):
    """American odds from ESPN (int, '-110', '+150', 'EVEN')."""
    if x is None:
        return None
    s = str(x).strip().upper()
    if s in ("EVEN", "EV"):
        return 100
    v = _num(s)
    return int(v) if v is not None and v != 0 else None


def _close(node):
    """ESPN newer odds: {'open': {...}, 'close': {...}, 'current': {...}} -> prefer close."""
    if not isinstance(node, dict):
        return {}
    for key in ("close", "current", "open"):
        if isinstance(node.get(key), dict) and node[key]:
            return node[key]
    return {}


def parse_odds(odds_list: list, home_abbr: str, away_abbr: str) -> dict:
    out = {k: None for k in ("spread_line", "total_line", "home_moneyline", "away_moneyline",
                             "home_spread_odds", "away_spread_odds", "over_odds", "under_odds")}
    if not odds_list:
        return out
    o = next((x for x in odds_list if x.get("details") or x.get("pointSpread")), odds_list[0])

    # spread: closing line for the home team -> nflverse convention (home favored = positive)
    home_line = _num(_close((o.get("pointSpread") or {}).get("home")).get("line"))
    if home_line is not None:
        out["spread_line"] = -home_line
    else:
        details = (o.get("details") or "").strip()
        if details.upper() in ("EVEN", "PK", "PICK"):
            out["spread_line"] = 0.0
        elif " " in details:
            abbr, _, num = details.rpartition(" ")
            v = _num(num)
            if v is not None:
                fav_line = abs(v)
                if abbr.upper() == (home_abbr or "").upper():
                    out["spread_line"] = fav_line
                elif abbr.upper() == (away_abbr or "").upper():
                    out["spread_line"] = -fav_line
        if out["spread_line"] is None and o.get("spread") is not None:
            out["spread_line"] = -float(o["spread"])  # ESPN 'spread' is the home team's line

    total = _num(_close((o.get("total") or {}).get("over")).get("line"))
    out["total_line"] = total if total is not None else _num(o.get("overUnder"))

    ml = o.get("moneyline") or {}
    hto, ato = o.get("homeTeamOdds") or {}, o.get("awayTeamOdds") or {}
    out["home_moneyline"] = _odds(_close(ml.get("home")).get("odds")) or _odds(hto.get("moneyLine"))
    out["away_moneyline"] = _odds(_close(ml.get("away")).get("odds")) or _odds(ato.get("moneyLine"))
    ps = o.get("pointSpread") or {}
    out["home_spread_odds"] = _odds(_close(ps.get("home")).get("odds")) or _odds(hto.get("spreadOdds"))
    out["away_spread_odds"] = _odds(_close(ps.get("away")).get("odds")) or _odds(ato.get("spreadOdds"))
    tot = o.get("total") or {}
    out["over_odds"] = _odds(_close(tot.get("over")).get("odds")) or _odds(o.get("overOdds"))
    out["under_odds"] = _odds(_close(tot.get("under")).get("odds")) or _odds(o.get("underOdds"))
    return out


def _rank(c):
    r = (c.get("curatedRank") or {}).get("current")
    try:
        r = int(r)
    except (TypeError, ValueError):
        return None
    return r if 1 <= r <= 25 else None


def parse_event(sport: str, ev: dict) -> dict | None:
    """One ESPN event -> one row for the games table (None = skip)."""
    comps = ev.get("competitions") or []
    if not comps:
        return None
    c = comps[0]
    season = ev.get("season") or {}
    stype = season.get("type")
    if stype == 1:  # preseason
        return None
    teams = {x.get("homeAway"): x for x in c.get("competitors") or []}
    home, away = teams.get("home"), teams.get("away")
    if not home or not away:
        return None
    status = ((c.get("status") or ev.get("status") or {}).get("type") or {})
    completed = bool(status.get("completed"))
    if status.get("state") == "post" and not completed:
        return None  # canceled / postponed
    when = ev.get("date") or c.get("date")
    try:
        dt = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(_ET)
    except (AttributeError, ValueError):
        return None
    ht, at = home.get("team") or {}, away.get("team") or {}
    h_abbr, a_abbr = ht.get("abbreviation"), at.get("abbreviation")
    venue = c.get("venue") or {}
    row = {
        "game_id": f"{sport}_{ev.get('id')}", "sport": sport,
        "season": season.get("year") or dt.year,
        "game_type": "REG" if stype in (2, None) else "POST",
        "week": (ev.get("week") or {}).get("number"),
        "gameday": dt.date().isoformat(), "weekday": dt.strftime("%A"), "gametime": dt.strftime("%H:%M"),
        "away_team": a_abbr, "home_team": h_abbr,
        "away_score": int(float(away["score"])) if completed and away.get("score") not in (None, "") else None,
        "home_score": int(float(home["score"])) if completed and home.get("score") not in (None, "") else None,
        "location": "Neutral" if c.get("neutralSite") else "Home",
        "roof": (("dome" if venue.get("indoor") else "outdoors") if sport in OUTDOOR_SPORTS else None),
        "temp": _num((ev.get("weather") or {}).get("temperature")),
        "stadium": venue.get("fullName"),
        "away_rank": _rank(away), "home_rank": _rank(home),
        "away_name": at.get("displayName") or at.get("location"),
        "home_name": ht.get("displayName") or ht.get("location"),
    }
    row.update(parse_odds(c.get("odds") or [], h_abbr, a_abbr))
    return row


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = ",".join(UPSERT_COLS)
    sets = ",".join(
        f"{col}=COALESCE(excluded.{col}, games.{col})" if col in KEEP_IF_NULL else f"{col}=excluded.{col}"
        for col in UPSERT_COLS if col != "game_id"
    )
    conn.executemany(
        f"INSERT INTO games ({cols}) VALUES ({','.join('?' * len(UPSERT_COLS))}) "
        f"ON CONFLICT(game_id) DO UPDATE SET {sets}",
        [tuple(r.get(c) for c in UPSERT_COLS) for r in rows],
    )
    conn.commit()
    return len(rows)


# ------------------------------------------------------------------ download loops
def season_window(sport: str, start_year: int) -> tuple[date, date]:
    cfg = SPORTS[sport]
    (sm, sd), (em, ed) = cfg["start"], cfg["end"]
    end_year = start_year + 1 if (em, ed) < (sm, sd) else start_year
    return date(start_year, sm, sd), date(end_year, em, ed)


def download_range(conn: sqlite3.Connection, sport: str, start: date, end: date, fetch=fetch_day,
                   pause: float = 0.15, progress=None) -> dict:
    if sport not in SPORTS:
        raise ValueError(f"Deporte desconocido: {sport}. Opciones: {', '.join(SPORTS)}")
    day, n_games, n_days, errors = start, 0, 0, []
    while day <= end:
        try:
            data = fetch(sport, day)
        except OSError as exc:
            errors.append(f"{day}: {exc}")
            if len(errors) >= 5 and n_games == 0:
                raise OSError(f"No pude conectar con ESPN ({exc})") from exc
            day += timedelta(days=1)
            continue
        rows = [r for r in (parse_event(sport, ev) for ev in data.get("events") or []) if r]
        n_games += upsert(conn, rows)
        n_days += 1
        if progress and rows:
            progress(day, len(rows))
        day += timedelta(days=1)
        if pause:
            time.sleep(pause)
    return {"sport": sport, "days": n_days, "games": n_games, "errors": errors}


def download_seasons(conn, sport: str, first: int, last: int, **kw) -> dict:
    total = {"sport": sport, "days": 0, "games": 0, "errors": []}
    today = date.today()
    for y in range(first, last + 1):
        start, end = season_window(sport, y)
        if start > today + timedelta(days=7):
            break
        res = download_range(conn, sport, start, min(end, today + timedelta(days=7)), **kw)
        for k in ("days", "games"):
            total[k] += res[k]
        total["errors"] += res["errors"]
    return total


def refresh_recent(conn, days_back: int = 7, days_ahead: int = 7, **kw) -> list[dict]:
    """Re-download the last/next few days for every ESPN sport already in the database."""
    sports = [r[0] for r in conn.execute("SELECT DISTINCT sport FROM games WHERE sport != 'nfl'")]
    today = date.today()
    return [download_range(conn, s, today - timedelta(days=days_back), today + timedelta(days=days_ahead), **kw)
            for s in sports if s in SPORTS]
