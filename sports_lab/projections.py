"""Train/backtest the projection model and produce weekly projections."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from . import model
from .db import DATA_DIR
from .report import BREAK_EVEN_110, p_value_one_sided, payout, z_score

PARAMS_PATH = DATA_DIR / "model_params.json"
GRADE_ORDER = ["A", "A-", "B+", "B", "B-", "C+", "C", "D+", "D", "F"]


def _seasons(games):
    return sorted({g.season for g in games if g.played})


def train(conn: sqlite3.Connection, test_from: int | None = None, first_season: int | None = None) -> dict:
    """Calibrate on seasons before `test_from`, evaluate on `test_from` onward, save parameters.

    The backtest is walk-forward: every game is projected with ratings fitted only on
    games played before its week, and parameters come only from the training seasons.
    """
    games = model.load_games(conn)
    seasons = _seasons(games)
    first_season = first_season or seasons[0] + 1  # the very first season has no history
    test_from = test_from or seasons[-1] - 7
    train_set = {s for s in seasons if first_season <= s < test_from}
    test_set = {s for s in seasons if s >= test_from}
    raw = model.raw_projections(games, train_set | test_set)
    params = model.calibrate(games, raw, train_set)
    mm = model.MarginModel([g for g in games if g.season in train_set])
    evaluation = evaluate(games, raw, params, mm, test_set)

    # for live use, refit on everything played so far (same method, more data)
    final = model.calibrate(games, raw, train_set | test_set)
    out = {"params": final.as_dict(), "backtest_params": params.as_dict(),
           "test_from": test_from, "evaluation": evaluation}
    PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PARAMS_PATH.write_text(json.dumps(out, indent=2))
    return out


def evaluate(games, raw, params, mm, seasons: set[int]) -> dict:
    """Record/ROI by market and grade on the given seasons, plus RMSE vs the closing line."""
    tallies = defaultdict(lambda: {"w": 0, "l": 0, "p": 0, "units": 0.0})
    sq_model = sq_mkt = sq_tm = sq_tk = 0.0
    n_sp = n_tot = 0
    for g in games:
        if g.season not in seasons or not g.played or g.game_id not in raw:
            continue
        pr = model.project_game(g, raw[g.game_id], params, mm)
        if g.spread is not None:
            sq_model += (g.margin + pr["proj_spread_home"]) ** 2
            sq_mkt += (g.margin - g.spread) ** 2
            n_sp += 1
        if g.total_line is not None:
            sq_tm += (g.total - pr["proj_total"]) ** 2
            sq_tk += (g.total - g.total_line) ** 2
            n_tot += 1
        # one bet per market per game: the side the model prefers
        best = {}
        for b in pr["bets"]:
            if b["market"] not in best or b["ev"] > best[b["market"]]["ev"]:
                best[b["market"]] = b
        for b in best.values():
            r = model.settle(g, b)
            t = tallies[(b["market"], b["grade"])]
            if r is None:
                t["p"] += 1
            elif r:
                t["w"] += 1
                t["units"] += payout(b["odds"])
            else:
                t["l"] += 1
                t["units"] -= 1

    rows = []
    for (market, grade), t in tallies.items():
        n = t["w"] + t["l"]
        z = z_score(t["w"], n, BREAK_EVEN_110)
        rows.append({"market": market, "grade": grade, **t, "units": round(t["units"], 2),
                     "pct": round(t["w"] / n, 4) if n else None,
                     "roi": round(t["units"] / n, 4) if n else None,
                     "p_value": round(p_value_one_sided(z), 4) if n else None})
    rows.sort(key=lambda r: (r["market"], GRADE_ORDER.index(r["grade"])))
    return {
        "seasons": sorted(seasons),
        "rmse_spread_model": round((sq_model / n_sp) ** 0.5, 3) if n_sp else None,
        "rmse_spread_market": round((sq_mkt / n_sp) ** 0.5, 3) if n_sp else None,
        "rmse_total_model": round((sq_tm / n_tot) ** 0.5, 3) if n_tot else None,
        "rmse_total_market": round((sq_tk / n_tot) ** 0.5, 3) if n_tot else None,
        "by_grade": rows,
    }


def load_params() -> model.Params | None:
    if not PARAMS_PATH.exists():
        return None
    d = json.loads(PARAMS_PATH.read_text())["params"]
    return model.Params(**d)


def weekly(conn: sqlite3.Connection, season: int | None = None, week: int | None = None) -> dict:
    """Projections for one slate (default: the next week with unplayed games)."""
    games = model.load_games(conn)
    if season is None or week is None:
        upcoming = [g for g in games if not g.played]
        if not upcoming:
            raise ValueError("No upcoming games in the database. Run `update`.")
        nxt = min(upcoming, key=lambda g: g.day)
        season, week = nxt.season, nxt.week
    params = load_params()
    if params is None:
        raise ValueError("Train the model first: python -m sports_lab train")
    slate = [g for g in games if g.season == season and g.week == week]
    if not slate:
        raise ValueError(f"No games for season {season} week {week}")
    as_of = min(g.day for g in slate)
    rt = model.fit_ratings([g for g in games if g.day < as_of], as_of)
    mm = model.MarginModel([g for g in games if g.played and g.season in set(params.train_seasons)] or games)
    out = []
    for g in slate:
        raw = {"sp_res": rt.spread("res", g.home, g.away, g.neutral),
               "sp_mkt": rt.spread("mkt", g.home, g.away, g.neutral),
               "tot_res": rt.total_res(g.home, g.away, g.neutral),
               "tot_mkt": rt.total_mkt_pred(g.home, g.away)}
        pr = model.project_game(g, raw, params, mm)
        pr["final"] = f"{g.away_score}-{g.home_score}" if g.played else None
        out.append(pr)
    return {"season": season, "week": week, "games": out, "params": params.as_dict()}


def power_table(conn: sqlite3.Connection) -> list[dict]:
    """Current power ratings (points vs an average team on a neutral field)."""
    games = model.load_games(conn)
    upcoming = [g for g in games if not g.played]
    as_of = min(g.day for g in upcoming) if upcoming else max(g.day for g in games)
    rt = model.fit_ratings([g for g in games if g.day < as_of], as_of)
    params = load_params() or model.Params()
    n = len(rt.teams)
    recent = [g for g in games if 0 <= (as_of - g.day).days <= 400 or g.season == games[-1].season]
    active = {g.home for g in recent} | {g.away for g in recent}  # skip relocated franchises (SD, OAK...)
    rows = []
    for t, i in rt.teams.items():
        if t not in active:
            continue
        res, mkt = rt.spread_res[i], rt.spread_mkt[i]
        rows.append({"team": t, "rating": round(params.w_spread * res + (1 - params.w_spread) * mkt, 1),
                     "results": round(res, 1), "market": round(mkt, 1),
                     "off": round(rt.pts_res[i], 1), "def": round(rt.pts_res[n + i], 1)})
    rows.sort(key=lambda r: -r["rating"])
    return rows


def format_weekly(res: dict) -> str:
    lines = [f"Projections season {res['season']} week {res['week']}", ""]
    for g in res["games"]:
        lines.append(f"{g['away']} @ {g['home']}  {g['date']}" + (f"  final {g['final']}" if g["final"] else "")
                     + (f"  wind {g['wind']:.0f} mph" if g["wind"] else ""))
        ms = g["market_spread_home"]
        lines.append(f"  Spread {g['home']}: PRO {g['proj_spread_home']:+.1f} (raw model {g['raw_spread_home']:+.1f})"
                     + " | market " + (f"{ms:+.1f}" if ms is not None else "—")
                     + f"    Total: PRO {g['proj_total']:.1f} (raw {g['raw_total']:.1f})"
                     + f" | market {g['market_total'] or '—'}"
                     + f"    fair ML {g['home']} {g['proj_ml_home']:+d} / {g['away']} {g['proj_ml_away']:+d}")
        for b in g["bets"]:
            team = g[b["side"]] if b["side"] in ("home", "away") else b["side"].capitalize()
            line = "" if b["line"] is None else (f"{b['line']:+.1f}" if b["market"] == "spread" else f"{b['line']}")
            lines.append(f"    {b['market']:<6} {team:<5} {line:>6} ({b['odds']:+d})  prob {b['prob']:.1%}  "
                         f"EV {b['ev']:+.1%}  grade {b['grade']}")
        lines.append("")
    return "\n".join(lines)


def format_eval(res: dict) -> str:
    e = res["evaluation"]
    p = res["backtest_params"]
    lines = [
        f"Walk-forward backtest: trained before {res['test_from']}, tested on {e['seasons'][0]}-{e['seasons'][-1]}",
        f"Params: results weight spread {p['w_spread']:.2f}, total {p['w_total']:.2f}; "
        f"k_spread {p['k_spread']:.2f}, k_total {p['k_total']:.2f}; wind vs market {p['wind_coef_mkt']:+.2f} pts/mph>8",
        f"Error (RMSE) spread: model {e['rmse_spread_model']} vs closing line {e['rmse_spread_market']}",
        f"Error (RMSE) total:  model {e['rmse_total_model']} vs closing line {e['rmse_total_market']}",
        "",
        "Out-of-sample record by grade (one bet per market per game):",
    ]
    for r in e["by_grade"]:
        n = r["w"] + r["l"]
        lines.append(f"  {r['market']:<6} {r['grade']:<3} {r['w']}-{r['l']}-{r['p']}  "
                     + (f"{r['pct']:.1%}  ROI {r['roi']:+.1%}  p={r['p_value']}" if n else ""))
    return "\n".join(lines)
