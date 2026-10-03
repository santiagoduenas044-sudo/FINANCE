"""Tests for sports_lab (run: python -m pytest sports_lab/tests)."""
import csv
import io
import random
from datetime import date, timedelta

import pytest

from sports_lab import db, ingest, model, projections, report, splits, weather
from sports_lab.query import QueryError, compile_query, to_sql

HEADER = ingest.GAME_COLS


def _csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in HEADER})
    return buf.getvalue()


def _game(season, week, day, away, home, a, h, spread, total, wind=5, roof="outdoors", **kw):
    g = dict(game_id=f"{season}_{week:02d}_{away}_{home}", season=season, game_type="REG", week=week,
             gameday=day, weekday="Sunday", gametime="13:00", away_team=away, home_team=home,
             away_score=a, home_score=h, location="Home", spread_line=spread, total_line=total,
             away_moneyline=150, home_moneyline=-170, away_spread_odds=-110, home_spread_odds=-110,
             under_odds=-110, over_odds=-110, away_rest=7, home_rest=7, div_game=0, roof=roof,
             surface="grass", temp=50, wind=wind, stadium_id="PIT00", stadium="Acrisure Stadium")
    g.update(kw)
    return g


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(projections, "PARAMS_PATH", tmp_path / "params.json")
    c = db.connect(tmp_path / "t.sqlite")
    rows = [
        # PIT home, favored by 3 (spread_line +3), wins by 7 -> covers; total 27 < 38 -> under
        _game(2020, 1, "2020-09-13", "CIN", "PIT", 10, 17, 3, 38, wind=15),
        # PIT away, dog +3 (spread_line +3 = home CLE favored), loses by 3 -> push
        _game(2020, 2, "2020-09-20", "PIT", "CLE", 20, 23, 3, 44, wind=4),
        # PIT home, favored by 7, wins by 3 -> no cover; total 47 > 41 -> over
        _game(2020, 3, "2020-09-27", "BAL", "PIT", 22, 25, 7, 41, wind=13),
        # upcoming game (no scores)
        _game(2020, 4, "2020-10-04", "PIT", "CIN", None, None, -2.5, 42, wind=None),
    ]
    ingest.update(c, _csv(rows))
    yield c
    c.close()


# ------------------------------------------------------------------ query language
def test_query_translates_and_binds():
    where, params = to_sql("total_line < 40 and wind >= 12 and outdoors")
    assert where == "((t.total_line < ?) AND (t.wind >= ?) AND (t.outdoors = 1))"
    assert params == [40, 12]


def test_query_text_fields_and_in_lists():
    where, params = to_sql("team in (pit, cle) and roof = outdoors and not home")
    assert "t.team COLLATE NOCASE IN (?, ?)" in where
    assert params == ["PIT", "CLE", "outdoors"]


def test_query_groups_and_field_comparison():
    c = compile_query("(dog or line = 0) and rest > opp_rest + 3")
    assert c.where == "(((t.dog = 1) OR (t.line = ?)) AND (t.rest > (o.rest + ?)))"
    assert c.interpreted == "(dog or line = 0) and rest > o:rest + 3"
    assert len(c.joins) == 1


def test_sdql_shorthands_and_prefixes():
    c = compile_query("team=pit and p:ATSW and H and season>2020 and op:L and o:team = NO")
    assert c.interpreted == "team = 'PIT' and p:covered and home and season > 2020 and op:lost and o:team = 'NO'"
    assert "(p.covered = 1)" in c.where and "(op.lost = 1)" in c.where
    assert c.params == ["PIT", 2020, "NO"]
    joined = " ".join(c.joins)
    assert "team_games p ON" in joined and "team_games o ON" in joined and "team_games op ON" in joined
    # O is the over shorthand, total is the posted total (SDQL), game_total the real points
    c = compile_query("O and total > game_total - 3")
    assert c.where == "((t.over = 1) AND (t.total_line > (t.total - ?)))"


@pytest.mark.parametrize("bad", ["winds > 3", "wind >", "wind > 3; DROP TABLE games", "team = PIT)", "1 = 1 or x",
                                 "x:won", "p:o:p:won", "team"])
def test_query_rejects_unknown_fields_and_junk(bad):
    with pytest.raises(QueryError):
        to_sql(bad)


# ------------------------------------------------------------------ team-perspective table
def test_team_games_derivations(conn):
    r = conn.execute("SELECT * FROM team_games WHERE game_id='2020_01_CIN_PIT' AND team='PIT'").fetchone()
    assert r["line"] == -3 and r["fav"] == 1 and r["home"] == 1
    assert r["margin"] == 7 and r["covered"] == 1 and r["under"] == 1
    r = conn.execute("SELECT * FROM team_games WHERE game_id='2020_02_PIT_CLE' AND team='PIT'").fetchone()
    assert r["line"] == 3 and r["dog"] == 1 and r["ats_push"] == 1 and r["covered"] is None
    assert r["p_won"] == 1 and r["p_covered"] == 1 and r["wins"] == 1 and r["streak"] == 1
    r = conn.execute("SELECT * FROM team_games WHERE game_id='2020_03_BAL_PIT' AND team='PIT'").fetchone()
    assert r["streak"] == -1 and r["ats_wins"] == 1 and r["ats_losses"] == 0
    assert r["op_won"] is None  # BAL has no earlier game


def test_report_counts_totals_once_per_game(conn):
    res = report.run(conn, "wind >= 12 and outdoors")
    assert res["games"] == 2 and res["rows"] == 4
    assert res["all"]["under"]["w"] == 1 and res["all"]["over"]["w"] == 1
    assert any("both teams" in w for w in res["warnings"])


def test_report_ats_and_units(conn):
    res = report.run(conn, "team = PIT")
    ats = res["all"]["ats"]
    assert (ats["w"], ats["l"], ats["p"]) == (1, 1, 1)
    assert ats["units"] == pytest.approx(100 / 110 - 1, abs=0.01)
    assert [g["opp"] for g in res["upcoming"]] == ["CIN"]


# ------------------------------------------------------------------ splits and line snapshots
def test_splits_manual_entry_feeds_queries(conn):
    gid = splits.add(conn, "PIT@CIN", home_bets_pct=30, home_money_pct=65)
    assert gid == "2020_04_PIT_CIN"
    r = conn.execute("SELECT bets_pct, money_pct FROM team_games WHERE game_id=? AND team='PIT'", (gid,)).fetchone()
    assert (r["bets_pct"], r["money_pct"]) == (70, 35)
    res = report.run(conn, "money_pct >= 60 and bets_pct <= 40")
    assert [g["team"] for g in res["upcoming"]] == ["CIN"]


def test_splits_csv_and_validation(conn):
    text = "game_id,season,week,home_bets_pct,home_money_pct\nCIN@PIT,2020,1,55,48\n"
    assert splits.import_csv(conn, text) == 1
    with pytest.raises(ValueError):
        splits.add(conn, "PIT@CIN", home_bets_pct=140)
    with pytest.raises(ValueError):
        splits.add(conn, "NYJ@MIA", home_bets_pct=40)


def test_line_snapshots_track_movement(conn):
    conn.execute("UPDATE games SET spread_line=-3.5 WHERE game_id='2020_04_PIT_CIN'")
    assert ingest.snapshot_lines(conn, "2099-01-01T00:00:00Z") == 1
    assert ingest.snapshot_lines(conn, "2099-01-02T00:00:00Z") == 0  # unchanged -> no new row
    ingest.build_team_games(conn)
    r = conn.execute("SELECT open_line, line, line_move FROM team_games WHERE game_id='2020_04_PIT_CIN' AND team='PIT'").fetchone()
    assert (r["open_line"], r["line"], r["line_move"]) == (-2.5, -3.5, -1.0)


# ------------------------------------------------------------------ weather
def test_weather_kickoff_average_and_storage(conn, monkeypatch):
    fake = {"hourly": {
        "time": ["2020-10-04T12:00", "2020-10-04T13:00", "2020-10-04T14:00", "2020-10-04T15:00"],
        "wind_speed_10m": [1, 10, 14, 18], "wind_gusts_10m": [2, 20, 22, 24],
        "temperature_2m": [40, 41, 42, 43], "precipitation": [0, 0.5, 0.5, 1.0]}}
    w = weather.at_kickoff(fake, "2020-10-04", "13:00")
    assert w == {"wind_mph": 14.0, "gust_mph": 22.0, "temp_f": 42.0, "precip_mm": 2.0}

    class FakeDT(weather.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2020, 10, 1, 12, 0)

    monkeypatch.setattr(weather, "datetime", FakeDT)
    conn.execute("UPDATE games SET stadium_id='CIN00' WHERE game_id='2020_04_PIT_CIN'")
    out = weather.update_forecasts(conn, fetch=lambda lat, lon: fake)
    assert out[0]["wind_mph"] == 14.0
    ingest.build_team_games(conn)
    assert report.run(conn, "wind_fcst >= 12")["upcoming"][0]["opp"] in ("PIT", "CIN")


# ------------------------------------------------------------------ model
def test_solve_small_system():
    x = model.solve([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0])
    assert x == pytest.approx([0.8, 1.4])


def _synthetic_league(seasons=4, teams=8, seed=0):
    rng = random.Random(seed)
    names = [f"T{i}" for i in range(teams)]
    strength = {t: rng.gauss(0, 5) for t in names}
    rows, day = [], date(2010, 9, 12)
    for s in range(2010, 2010 + seasons):
        day = date(s, 9, 12)
        for wk in range(1, 15):
            order = names[:]
            rng.shuffle(order)
            for i in range(0, teams, 2):
                home, away = order[i], order[i + 1]
                true = strength[home] - strength[away] + 2
                spread = round((true + rng.gauss(0, 1)) * 2) / 2
                margin = round(true + rng.gauss(0, 12))
                total_true = 44 + rng.gauss(0, 2)
                wind = rng.choice([0, 3, 6, 10, 15, 20])
                tot = max(6, round(total_true - 0.3 * max(0, wind - 8) + rng.gauss(0, 10)))
                hs = max(0, (tot + margin) // 2)
                as_ = max(0, tot - hs)
                rows.append(_game(s, wk, day.isoformat(), away, home, as_, hs, spread,
                                  round(total_true * 2) / 2, wind=wind))
            day += timedelta(days=7)
    return rows


def test_projection_pipeline_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(projections, "PARAMS_PATH", tmp_path / "params.json")
    c = db.connect(tmp_path / "m.sqlite")
    rows = _synthetic_league()
    rows.append(_game(2014, 1, "2014-09-14", "T1", "T2", None, None, 1.5, 44, wind=None))
    ingest.update(c, _csv(rows))
    out = projections.train(c, test_from=2013)
    p = out["params"]
    assert 0 <= p["k_spread"] <= 1 and 0 <= p["k_total"] <= 1
    assert out["evaluation"]["rmse_spread_model"] > 0
    wk = projections.weekly(c)
    assert (wk["season"], wk["week"]) == (2014, 1)
    g = wk["games"][0]
    assert {b["market"] for b in g["bets"]} == {"spread", "total", "ml"}
    for b in g["bets"]:
        assert 0 < b["prob"] < 1 and b["grade"] in projections.GRADE_ORDER
    assert len(projections.power_table(c)) == 8


def test_margin_model_is_fair_at_the_line():
    games = [model.Game(f"g{i}", 2010, 1, date(2010, 9, 12), "A", "B", False, h, a, s, 44, None, None,
                        None, None, None, None, True, 0)
             for i, (h, a, s) in enumerate([(24, 21, 3), (17, 10, 7), (20, 17, 3), (10, 13, -3), (27, 24, 2.5)] * 40)]
    mm = model.MarginModel(games)
    win, push = mm.cover_prob(3.5, -3.5)
    assert win == pytest.approx(0.5, abs=1e-6) and push == 0
    win, push = mm.cover_prob(3.0, -3.0)
    assert push > 0.05  # key number: pushes on 3 are common
    assert win == pytest.approx((1 - push) / 2, abs=1e-6)
    assert mm.win_prob(7) > mm.win_prob(3) > 0.5


def test_settle_and_grade():
    g = model.Game("x", 2020, 1, date(2020, 9, 13), "H", "A", False, 24, 20, 3.0, 45, -150, 130,
                   -110, -110, -110, -110, True, 0)
    assert model.settle(g, {"market": "spread", "side": "home", "line": -3.0}) is True
    assert model.settle(g, {"market": "spread", "side": "away", "line": 4.0}) is None
    assert model.settle(g, {"market": "total", "side": "under", "line": 45}) is True
    assert model.grade(0.1) == "A" and model.grade(-0.045) == "D" and model.grade(-0.2) == "F"
