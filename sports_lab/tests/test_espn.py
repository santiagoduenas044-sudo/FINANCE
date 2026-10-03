"""ESPN scoreboard parsing and multi-sport queries, using recorded-shape fixtures (no network)."""
from datetime import date

import pytest

from sports_lab import db, espn, ingest, report


def _team(abbr, name, score=None, rank=99, home=True):
    return {"homeAway": "home" if home else "away", "score": None if score is None else str(score),
            "team": {"abbreviation": abbr, "displayName": name}, "curatedRank": {"current": rank}}


def _event(eid, when, home, away, odds=None, completed=True, season=2025, stype=2, week=3, neutral=False,
           state=None):
    return {
        "id": str(eid), "date": when, "season": {"year": season, "type": stype}, "week": {"number": week},
        "competitions": [{
            "neutralSite": neutral, "venue": {"fullName": "Sanford Stadium", "indoor": False},
            "competitors": [home, away],
            "status": {"type": {"completed": completed, "state": state or ("post" if completed else "pre")}},
            "odds": odds or [],
        }],
    }


OLD_STYLE = [{"details": "UGA -24.5", "overUnder": 50.5,
              "homeTeamOdds": {"moneyLine": -4500, "spreadOdds": -110},
              "awayTeamOdds": {"moneyLine": 1800, "spreadOdds": -110}}]
NEW_STYLE = [{"details": "TEX -3.5",
              "pointSpread": {"home": {"open": {"line": "+2.5"}, "close": {"line": "+3.5", "odds": "-105"}},
                              "away": {"close": {"line": "-3.5", "odds": "-115"}}},
              "total": {"over": {"close": {"line": "o44.5", "odds": "-110"}},
                        "under": {"close": {"line": "u44.5", "odds": "-110"}}},
              "moneyline": {"home": {"close": {"odds": "+140"}}, "away": {"close": {"odds": "EVEN"}}}}]


def test_parse_old_style_odds_and_ranks():
    ev = _event(1, "2025-09-13T16:00Z", _team("UGA", "Georgia Bulldogs", 42, rank=2),
                _team("VAN", "Vanderbilt Commodores", 10, home=False), OLD_STYLE)
    row = espn.parse_event("cfb", ev)
    assert row["game_id"] == "cfb_1" and row["season"] == 2025 and row["week"] == 3
    assert row["gameday"] == "2025-09-13" and row["gametime"] == "12:00"  # 16:00Z = noon ET
    assert row["spread_line"] == 24.5  # home favored -> positive (nflverse convention)
    assert row["total_line"] == 50.5 and row["home_moneyline"] == -4500 and row["away_moneyline"] == 1800
    assert (row["home_rank"], row["away_rank"]) == (2, None)
    assert (row["home_score"], row["away_score"]) == (42, 10)


def test_parse_new_style_prefers_closing_numbers():
    ev = _event(2, "2025-10-04T23:30Z", _team("OU", "Oklahoma Sooners", 17), _team("TEX", "Texas Longhorns", 24, home=False),
                NEW_STYLE)
    row = espn.parse_event("cfb", ev)
    assert row["spread_line"] == -3.5  # home +3.5 = away favored
    assert row["total_line"] == 44.5
    assert row["home_moneyline"] == 140 and row["away_moneyline"] == 100
    assert row["home_spread_odds"] == -105 and row["over_odds"] == -110


def test_parse_skips_preseason_and_canceled():
    pre = _event(3, "2025-08-10T00:00Z", _team("A", "A"), _team("B", "B", home=False), stype=1)
    canceled = _event(4, "2025-09-01T00:00Z", _team("A", "A"), _team("B", "B", home=False), completed=False, state="post")
    assert espn.parse_event("nba", pre) is None and espn.parse_event("nba", canceled) is None
    upcoming = espn.parse_event("nba", _event(5, "2025-11-01T23:00Z", _team("BOS", "Celtics"),
                                                _team("NY", "Knicks", home=False), completed=False))
    assert upcoming["home_score"] is None and upcoming["roof"] is None


def test_season_window_wraps_new_year():
    assert espn.season_window("nba", 2025) == (date(2025, 10, 1), date(2026, 6, 30))
    assert espn.season_window("mlb", 2025) == (date(2025, 3, 15), date(2025, 11, 5))


@pytest.fixture
def multi(tmp_path):
    c = db.connect(tmp_path / "m.sqlite")
    nfl_csv = ("game_id,season,game_type,week,gameday,gametime,away_team,home_team,away_score,home_score,location,"
               "spread_line,total_line\n"
               "2025_01_CLE_PIT,2025,REG,1,2025-09-07,13:00,CLE,PIT,10,20,Home,3,40\n")
    ingest.update(c, nfl_csv)
    days = {
        date(2025, 9, 13): [_event(10, "2025-09-13T16:00Z", _team("UGA", "Georgia", 42, rank=2),
                                   _team("VAN", "Vanderbilt", 10, home=False), OLD_STYLE)],
        date(2025, 9, 20): [_event(11, "2025-09-20T16:00Z", _team("UGA", "Georgia", 20, rank=2),
                                   _team("TEX", "Texas", 24, rank=10, home=False), NEW_STYLE)],
        date(2025, 9, 27): [_event(12, "2025-09-27T16:00Z", _team("UGA", "Georgia", rank=5),
                                   _team("VAN", "Vanderbilt", home=False), OLD_STYLE, completed=False)],
    }
    res = espn.download_range(c, "cfb", date(2025, 9, 13), date(2025, 9, 27),
                              fetch=lambda sport, d: {"events": days.get(d, [])}, pause=0)
    assert res["games"] == 3
    # an NHL team sharing the PIT code must not mix with the NFL Steelers
    nhl = [_event(20, "2025-10-09T23:00Z", _team("PIT", "Penguins", 1), _team("NYR", "Rangers", 4, home=False))]
    espn.download_range(c, "nhl", date(2025, 10, 9), date(2025, 10, 9),
                        fetch=lambda sport, d: {"events": nhl}, pause=0)
    ingest.build_team_games(c)
    yield c
    c.close()


def test_multi_sport_queries(multi):
    res = report.run(multi, "team = UGA and H", sport="cfb")
    assert res["all"]["su"]["w"] == 1 and res["all"]["su"]["l"] == 1
    assert res["all"]["ats"]["w"] == 1 and res["all"]["ats"]["l"] == 1  # covered -24.5, lost as +3.5 dog
    assert [g["opp"] for g in res["upcoming"]] == ["VAN"]
    nxt = res["upcoming"][0]
    assert nxt["rank"] == 5
    up = multi.execute("SELECT rest, p_won FROM team_games WHERE game_id='cfb_12' AND team='UGA'").fetchone()
    assert up["rest"] == 7 and up["p_won"] == 0  # rest computed from dates for ESPN sports

    ranked = report.run(multi, "ranked and o:ranked", sport="cfb")
    assert ranked["games"] == 1  # only UGA vs TEX had both teams ranked

    assert report.run(multi, "team = PIT", sport="nfl")["all"]["su"]["w"] == 1
    nhl = report.run(multi, "team = PIT", sport="nhl")
    assert nhl["all"]["su"]["l"] == 1 and nhl["rows"] == 1


def test_upsert_keeps_line_when_espn_drops_it(multi):
    ev = _event(12, "2025-09-27T16:00Z", _team("UGA", "Georgia", 31, rank=5), _team("VAN", "Vanderbilt", 7, home=False))
    espn.upsert(multi, [espn.parse_event("cfb", ev)])  # final score, but no odds this time
    g = multi.execute("SELECT home_score, spread_line, total_line FROM games WHERE game_id='cfb_12'").fetchone()
    assert (g["home_score"], g["spread_line"], g["total_line"]) == (31, 24.5, 50.5)


def test_teasers_shift_the_number(multi):
    res = report.run(multi, "team = UGA and H", sport="cfb")
    t = {x["points"]: x for x in res["all"]["teasers"]}
    # ATS margins: +7.5 (won 42-10 at -24.5) and -0.5 (lost by 4 as +3.5 dog)
    assert (t[6]["ats"]["w"], t[6]["ats"]["l"]) == (2, 0)
    assert (t[-10]["ats"]["w"], t[-10]["ats"]["l"]) == (0, 2)
    # totals 52 vs 50.5 (+1.5) and 44 vs 44.5 (-0.5): +6 makes both unders
    assert (t[6]["under"]["w"], t[6]["over"]["w"]) == (2, 0)
