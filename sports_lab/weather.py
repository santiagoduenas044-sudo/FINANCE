"""Kickoff weather forecasts for upcoming outdoor games (Open-Meteo, free, no API key)."""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .ingest import now_iso

# nflverse stadium_id -> (lat, lon). Domes are listed too so neutral-site
# lookups work; the roof column decides whether wind matters.
STADIUMS: dict[str, tuple[float, float]] = {
    "PHO00": (33.5276, -112.2626), "ATL97": (33.7554, -84.4008), "BAL00": (39.2780, -76.6227),
    "BUF00": (42.7738, -78.7870), "BUF01": (42.7738, -78.7870), "CAR00": (35.2258, -80.8528),
    "CHI98": (41.8623, -87.6167), "CIN00": (39.0955, -84.5161), "CLE00": (41.5061, -81.6995),
    "DAL00": (32.7473, -97.0945), "DEN00": (39.7439, -105.0201), "DET00": (42.3400, -83.0456),
    "GNB00": (44.5013, -88.0622), "HOU00": (29.6847, -95.4107), "IND00": (39.7601, -86.1639),
    "JAX00": (30.3239, -81.6373), "KAN00": (39.0489, -94.4839), "LAX01": (33.9535, -118.3392),
    "VEG00": (36.0909, -115.1833), "MIA00": (25.9580, -80.2389), "MIN01": (44.9738, -93.2581),
    "BOS00": (42.0909, -71.2643), "NOR00": (29.9511, -90.0812), "NYC01": (40.8135, -74.0745),
    "PHI00": (39.9008, -75.1675), "PIT00": (40.4468, -80.0158), "SFO01": (37.4030, -121.9700),
    "SEA00": (47.5952, -122.3316), "TAM00": (27.9759, -82.5033), "NAS00": (36.1665, -86.7713),
    "WAS00": (38.9077, -76.8645),
    # frequent international / neutral venues
    "LON00": (51.5560, -0.2796), "LON02": (51.6043, -0.0664), "MEX00": (19.3029, -99.1505),
    "MUN01": (48.2188, 11.6247), "FRA00": (50.0686, 8.6455), "MAD01": (40.4531, -3.6883),
    "SAO00": (-23.5453, -46.4742), "RIO00": (-22.9121, -43.2302), "DUB00": (53.3353, -6.2285),
    "MEL00": (-37.8200, 144.9834), "PAR00": (48.9245, 2.3602),
}

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_forecast(lat: float, lon: float, timeout: int = 30) -> dict:
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "wind_speed_10m,wind_gusts_10m,temperature_2m,precipitation",
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",  # nflverse kickoff times are Eastern
        "forecast_days": 16,
    }
    url = FORECAST_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "sports-lab/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def at_kickoff(forecast: dict, gameday: str, gametime: str | None) -> dict | None:
    """Average of the kickoff hour and the next two hours (most of a game)."""
    hourly = forecast.get("hourly") or {}
    times = hourly.get("time") or []
    kick = datetime.fromisoformat(f"{gameday}T{(gametime or '13:00')[:5]}")
    kick = kick.replace(minute=0)
    wanted = {(kick + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(3)}
    idx = [i for i, t in enumerate(times) if t in wanted]
    if not idx:
        return None

    def avg(key):
        vals = [hourly[key][i] for i in idx if hourly.get(key) and hourly[key][i] is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    precip = hourly.get("precipitation")
    return {
        "wind_mph": avg("wind_speed_10m"), "gust_mph": avg("wind_gusts_10m"),
        "temp_f": avg("temperature_2m"),
        "precip_mm": round(sum(precip[i] or 0 for i in idx), 1) if precip else None,
    }


def update_forecasts(conn: sqlite3.Connection, days_ahead: int = 10, fetch=fetch_forecast) -> list[dict]:
    """Fetch forecasts for unplayed outdoor games in the next `days_ahead` days."""
    today = datetime.now().date()
    horizon = (today + timedelta(days=days_ahead)).isoformat()
    games = conn.execute(
        """SELECT game_id, gameday, gametime, stadium_id, roof, home_team, away_team FROM games
           WHERE sport = 'nfl' AND home_score IS NULL AND gameday >= ? AND gameday <= ?""",
        (today.isoformat(), horizon),
    ).fetchall()
    captured = now_iso()
    cache: dict[str, dict] = {}
    out = []
    for g in games:
        if g["roof"] in ("dome", "closed"):
            continue  # indoors: weather irrelevant
        loc = STADIUMS.get(g["stadium_id"])
        if loc is None:
            out.append({"game_id": g["game_id"], "error": f"sin coordenadas para {g['stadium_id']}"})
            continue
        if g["stadium_id"] not in cache:
            cache[g["stadium_id"]] = fetch(*loc)
        w = at_kickoff(cache[g["stadium_id"]], g["gameday"], g["gametime"])
        if w is None:
            continue
        conn.execute("INSERT OR REPLACE INTO weather_forecasts VALUES (?,?,?,?,?,?)",
                     (g["game_id"], captured, w["wind_mph"], w["gust_mph"], w["temp_f"], w["precip_mm"]))
        out.append({"game_id": g["game_id"], "matchup": f"{g['away_team']} @ {g['home_team']}",
                    "roof": g["roof"] or "retractable/?", **w})
    conn.commit()
    return out
