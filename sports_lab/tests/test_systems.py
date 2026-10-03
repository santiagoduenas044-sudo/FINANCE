"""Systems library, starter packs, board, tailing and automatic grading."""
from datetime import date, timedelta

import pytest

from sports_lab import db, ingest, systems
from sports_lab.query import QueryError

HEAD = ("game_id,season,game_type,week,gameday,gametime,away_team,home_team,away_score,home_score,location,"
        "spread_line,total_line,away_moneyline,home_moneyline,home_spread_odds,away_spread_odds,under_odds,"
        "over_odds,roof,wind\n")


def _csv(upcoming_day: date, final=None):
    hs, as_ = final or ("", "")
    d = upcoming_day.isoformat()
    return HEAD + (
        "2025_01_CLE_PIT,2025,REG,1,2025-09-07,13:00,CLE,PIT,10,13,Home,3,38,140,-160,-110,-110,-110,-110,outdoors,18\n"
        "2025_02_PIT_BAL,2025,REG,2,2025-09-14,13:00,PIT,BAL,20,17,Home,-2.5,41,-130,110,-110,-110,-110,-110,outdoors,4\n"
        f"2025_03_CIN_PIT,2025,REG,3,{d},13:00,CIN,PIT,{as_},{hs},Home,3.5,42.5,150,-180,-115,-105,-108,-112,outdoors,\n"
    )


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.sqlite")
    ingest.update(c, _csv(date.today() + timedelta(days=1)))
    yield c
    c.close()


def test_create_validates(conn):
    sid = systems.create(conn, "PIT local", "nfl", "team = PIT and H", "ats")
    assert sid > 0
    with pytest.raises(QueryError):
        systems.create(conn, "malo", "nfl", "winds > 3", "ats")
    with pytest.raises(ValueError):
        systems.create(conn, "x", "nfl", "H", "parlay")
    with pytest.raises(ValueError):
        systems.create(conn, "  ", "nfl", "H", "ats")


def test_list_and_stats(conn):
    systems.create(conn, "PIT local", "nfl", "team = PIT and H", "ats")
    systems.create(conn, "Viento", "nfl", "wind >= 15 and outdoors", "under")
    items = {s["name"]: s for s in systems.list_systems(conn, sort="roi")}
    pit = items["PIT local"]["record"]
    assert (pit["w"], pit["l"], pit["p"]) == (0, 0, 1)  # won 13-10 as a 3-point favorite: push
    assert items["PIT local"]["upcoming"] == 1
    assert items["Viento"]["record"]["w"] == 1  # 23 < 38 under, counted once per game
    assert systems.list_systems(conn, search="viento")[0]["name"] == "Viento"
    assert systems.list_systems(conn, sport="nba") == []


def test_packs_add_once(conn):
    n = systems.add_pack(conn, "nfl-clima")
    assert n == len(systems.PACKS[0]["systems"])
    assert systems.add_pack(conn, "nfl-clima") == 0
    with pytest.raises(ValueError):
        systems.add_pack(conn, "nope")


def test_board_tail_pass_and_grade(conn):
    sid = systems.create(conn, "PIT local", "nfl", "team = PIT and H", "ats")
    tid = systems.create(conn, "Total bajo", "nfl", "total < 45", "under")
    cards = systems.board(conn)
    assert {c["system"] for c in cards} == {"PIT local", "Total bajo"}
    under = next(c for c in cards if c["bet"] == "under")
    assert under["pick"]["label"] == "Under 42.5" and under["pick"]["odds"] == -108
    ats = next(c for c in cards if c["bet"] == "ats")
    assert ats["pick"]["label"] == "PIT -3.5" and ats["pick"]["odds"] == -115

    systems.tail(conn, sid, ats["game"]["game_id"], "PIT")
    systems.pass_card(conn, tid, under["game"]["game_id"], under["game"]["team"])
    assert systems.board(conn) == []  # both cards handled

    # line moves after you tailed: your pick keeps the number you got
    conn.execute("UPDATE games SET spread_line = 6.5 WHERE game_id='2025_03_CIN_PIT'")
    ingest.update(conn, _csv(date.today() + timedelta(days=1), final=(24, 20)))  # PIT wins by 4
    pf = systems.portfolio(conn)
    pick = pf["picks"][0]
    assert pick["line"] == -3.5 and pick["result"] == "W"
    assert pick["units"] == pytest.approx(100 / 115, abs=1e-3)
    assert pf["total"]["w"] == 1 and pf["total"]["pending"] == 0
    assert pf["by_system"][0]["system"] == "PIT local"


def test_cannot_tail_finished_or_lineless(conn):
    sid = systems.create(conn, "PIT", "nfl", "team = PIT", "ats")
    with pytest.raises(ValueError):
        systems.tail(conn, sid, "2025_01_CLE_PIT", "PIT")
    conn.execute("UPDATE games SET spread_line = NULL WHERE game_id='2025_03_CIN_PIT'")
    ingest.build_team_games(conn)
    with pytest.raises(ValueError):
        systems.tail(conn, sid, "2025_03_CIN_PIT", "PIT")


@pytest.mark.parametrize("bet,line,pts,opp,expected", [
    ("ats", 3.5, 20, 23, "W"), ("ats", -3, 23, 20, "P"), ("ats", -7, 23, 20, "L"),
    ("ml", None, 10, 9, "W"), ("over", 41.5, 21, 21, "W"), ("under", 42, 21, 21, "P"), ("under", 40.5, 21, 21, "L"),
])
def test_grade(bet, line, pts, opp, expected):
    assert systems.grade(bet, line, -110, pts, opp)[0] == expected
