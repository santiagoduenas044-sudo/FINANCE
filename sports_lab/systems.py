"""Saved systems, starter packs, the daily board and your tailed-picks portfolio.

A *system* is a query plus what to bet when a game matches it (ATS or moneyline on
the row's team, or the game's over/under). The board lists upcoming games that
match your systems; tailing one stores the line and price at that moment, and the
pick is graded automatically once the game is final.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import report
from .ingest import now_iso
from .query import compile_query
from .report import BREAK_EVEN_110, payout

BETS = {"ats": "ATS", "ml": "Moneyline", "over": "Over", "under": "Under"}
SPORTS = ("nfl", "cfb", "nba", "cbb", "nhl", "mlb")

# Starter packs. Records are always computed live from your database, never hard-coded.
PACKS = [
    {"id": "nfl-clima", "sport": "nfl", "name": "NFL Weather Desk",
     "desc": "Wind and cold: the market under-adjusts totals for outdoor games in bad weather.",
     "systems": [
         {"name": "Under in strong wind", "query": "wind >= 15 and outdoors", "bet": "under"},
         {"name": "Low total + wind under", "query": "total < 40 and wind >= 12 and outdoors", "bet": "under"},
         {"name": "Extreme cold under", "query": "temp <= 25 and outdoors", "bet": "under"},
         {"name": "Forecast wind under", "query": "wind_fcst >= 15 and outdoors", "bet": "under",
          "notes": "Upcoming games only: uses the Open-Meteo forecast."},
     ]},
    {"id": "nfl-situacional", "sport": "nfl", "name": "NFL Situational Desk",
     "desc": "Bounce-backs and rest: dogs after a blowout, favorites off a bye, big division dogs.",
     "systems": [
         {"name": "Road dog after a blowout", "query": "A and D and p:margin <= -14", "bet": "ats"},
         {"name": "Home dog after a loss", "query": "H and D and p:L", "bet": "ats"},
         {"name": "Favorite off a bye", "query": "F and rest >= 13", "bet": "ats"},
         {"name": "Division dog of 7+", "query": "div and line >= 7", "bet": "ats"},
         {"name": "Home team with a rest edge", "query": "H and rest >= o:rest + 3", "bet": "ats"},
     ]},
    {"id": "cfb-rankings", "sport": "cfb", "name": "CFB Rankings Desk",
     "desc": "Games between ranked teams (run `download cfb` first).",
     "systems": [
         {"name": "Ranked dog vs ranked", "query": "D and ranked and o:ranked", "bet": "ats"},
         {"name": "Top-10 home favorite of 21+", "query": "H and rank <= 10 and line <= -21", "bet": "ats"},
     ]},
    {"id": "nba-descanso", "sport": "nba", "name": "NBA Rest Desk",
     "desc": "Back-to-backs and rest edges (run `download nba` first).",
     "systems": [
         {"name": "Road team on a back-to-back vs rested", "query": "A and rest = 1 and o:rest >= 2", "bet": "ats"},
         {"name": "Rested home team vs back-to-back", "query": "H and rest >= 2 and o:rest = 1", "bet": "ats"},
     ]},
    {"id": "nhl-rebote", "sport": "nhl", "name": "NHL Bounce-back Desk",
     "desc": "Home dogs after a loss (run `download nhl` first).",
     "systems": [
         {"name": "Home dog after a loss (ML)", "query": "H and D and p:L", "bet": "ml"},
     ]},
]


# ------------------------------------------------------------------ CRUD
def create(conn: sqlite3.Connection, name: str, sport: str, query: str, bet: str,
           notes: str | None = None, pack: str | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the system a name")
    if sport not in SPORTS:
        raise ValueError(f"Unknown sport: {sport}")
    if bet not in BETS:
        raise ValueError("Bet must be ats, ml, over or under")
    compile_query(query)  # raises QueryError (a ValueError) if the query is invalid
    cur = conn.execute(
        "INSERT INTO systems (name, sport, query, bet, notes, pack, created_at) VALUES (?,?,?,?,?,?,?)",
        (name, sport, query.strip(), bet, notes, pack, now_iso()))
    conn.commit()
    return cur.lastrowid


def delete(conn, system_id: int) -> None:
    conn.execute("DELETE FROM systems WHERE id=?", (system_id,))
    conn.execute("DELETE FROM passes WHERE system_id=?", (system_id,))
    conn.commit()


def toggle_like(conn, system_id: int) -> None:
    conn.execute("UPDATE systems SET liked = 1 - COALESCE(liked, 0) WHERE id=?", (system_id,))
    conn.commit()


def add_pack(conn, pack_id: str) -> int:
    pack = next((p for p in PACKS if p["id"] == pack_id), None)
    if pack is None:
        raise ValueError(f"Unknown pack: {pack_id}")
    have = {(r["sport"], r["query"], r["bet"]) for r in conn.execute("SELECT sport, query, bet FROM systems")}
    n = 0
    for s in pack["systems"]:
        if (pack["sport"], s["query"], s["bet"]) in have:
            continue
        create(conn, s["name"], pack["sport"], s["query"], s["bet"], s.get("notes"), pack["id"])
        n += 1
    return n


# ------------------------------------------------------------------ stats
def _record_for(summary: dict, bet: str) -> dict:
    return summary["ats" if bet == "ats" else "ml" if bet == "ml" else bet]


def stats(conn, system: sqlite3.Row | dict) -> dict:
    res = report.run(conn, system["query"], sport=system["sport"], limit_games=0, series_points=48)
    rec = _record_for(res["all"], system["bet"])
    out = {"record": rec, "games": res["games"], "upcoming": res["upcoming"], "interpreted": res["interpreted"],
           "spark": [v for _d, v in res["series"][system["bet"]]]}
    split = res.get("split")
    test = _record_for(split["test"], system["bet"]) if split else None
    train = _record_for(split["train"], system["bet"]) if split else None
    out["split"] = {"test_from": split["test_from"], "train": train, "test": test} if split else None
    # "Proven": a real sample, unlikely to be luck, profitable overall and both before and after the split
    out["proven"] = bool(
        rec["n"] >= 40 and (rec["roi"] or 0) > 0 and (rec["p_value"] if rec["p_value"] is not None else 1) <= 0.15
        and (rec["pct"] or 0) > (0.5 if system["bet"] == "ml" else BREAK_EVEN_110)
        and (split is None or ((train["roi"] or 0) > 0 and test["n"] >= 10 and (test["roi"] or 0) > 0))
    )
    return out


def list_systems(conn, sport: str | None = None, sort: str = "newest", search: str | None = None) -> list[dict]:
    rows = conn.execute("SELECT * FROM systems ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        if sport and r["sport"] != sport:
            continue
        if search and search.lower() not in f"{r['name']} {r['query']} {r['notes'] or ''}".lower():
            continue
        item = dict(r)
        try:
            st = stats(conn, r)
        except ValueError as exc:
            item.update(error=str(exc), record=None, proven=False, upcoming=0)
        else:
            item.update(record=st["record"], games=st["games"], split=st["split"], proven=st["proven"],
                        upcoming=len(st["upcoming"]), interpreted=st["interpreted"], spark=st["spark"])
        out.append(item)
    key = {
        "roi": lambda x: -((x.get("record") or {}).get("roi") or -9),
        "winrate": lambda x: -((x.get("record") or {}).get("pct") or 0),
        "games": lambda x: -((x.get("record") or {}).get("n") or 0),
        "liked": lambda x: (-(x.get("liked") or 0), -x["id"]),
        "newest": lambda x: -x["id"],
    }.get(sort, lambda x: -x["id"])
    return sorted(out, key=key)


# ------------------------------------------------------------------ board
def describe_bet(bet: str, row: dict) -> dict:
    """What exactly you'd bet on this row right now: label, line and price."""
    if bet == "ats":
        line, odds = row["line"], row.get("spread_odds") or -110
        label = f"{row['team']} {line:+g}" if line is not None else f"{row['team']} (no line)"
    elif bet == "ml":
        line, odds = None, row.get("ml")
        label = f"{row['team']} ML" + (f" {odds:+d}" if odds else "")
    else:
        line = row["total_line"]
        odds = row.get(f"{bet}_odds") or -110
        label = f"{BETS[bet]} {line:g}" if line is not None else f"{BETS[bet]} (no total)"
    return {"label": label, "line": line, "odds": odds, "available": line is not None or (bet == "ml" and odds)}


def board(conn, days: int = 7, sport: str | None = None, proven_only: bool = False,
          today_only: bool = False, sort: str = "proven") -> list[dict]:
    today = date.today()
    horizon = today if today_only else today + timedelta(days=days)
    passed = {(r[0], r[1], r[2]) for r in conn.execute("SELECT system_id, game_id, team FROM passes")}
    tailed = {(r[0], r[1], r[2]) for r in conn.execute("SELECT system_id, game_id, team FROM picks")}
    cards = []
    for s in conn.execute("SELECT * FROM systems").fetchall():
        if sport and s["sport"] != sport:
            continue
        try:
            st = stats(conn, s)
        except ValueError:
            continue
        if proven_only and not st["proven"]:
            continue
        seen_games = set()
        for g in st["upcoming"]:
            if not g["date"] or not (today.isoformat() <= g["date"] <= horizon.isoformat()):
                continue
            if s["bet"] in ("over", "under"):  # one card per game for totals
                if g["game_id"] in seen_games:
                    continue
                seen_games.add(g["game_id"])
            key = (s["id"], g["game_id"], g["team"])
            if key in passed or key in tailed:
                continue
            cards.append({
                "system_id": s["id"], "system": s["name"], "sport": s["sport"], "query": s["query"],
                "bet": s["bet"], "pack": s["pack"], "record": st["record"], "proven": st["proven"],
                "split": st["split"], "spark": st["spark"], "game": g, "pick": describe_bet(s["bet"], g),
            })
    order = {
        "proven": lambda c: (not c["proven"], -(c["record"]["roi"] or -9)),
        "roi": lambda c: -(c["record"]["roi"] or -9),
        "winrate": lambda c: -(c["record"]["pct"] or 0),
        "games": lambda c: -c["record"]["n"],
        "date": lambda c: (c["game"]["date"], c["game"].get("hour") or 0),
    }.get(sort, lambda c: (c["game"]["date"],))
    return sorted(cards, key=order)


def tail(conn, system_id: int, game_id: str, team: str, stake: float = 1.0) -> int:
    s = conn.execute("SELECT * FROM systems WHERE id=?", (system_id,)).fetchone()
    if s is None:
        raise ValueError("System not found")
    row = conn.execute("SELECT * FROM team_games WHERE game_id=? AND team=?", (game_id, team)).fetchone()
    if row is None:
        raise ValueError("Game not found")
    if row["won"] is not None:
        raise ValueError("That game is already final")
    pick = describe_bet(s["bet"], dict(row))
    if not pick["available"]:
        raise ValueError("There is no line for that game yet")
    cur = conn.execute(
        "INSERT OR IGNORE INTO picks (system_id, game_id, team, bet, line, odds, stake, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (system_id, game_id, team, s["bet"], pick["line"], pick["odds"], stake, now_iso()))
    conn.commit()
    return cur.lastrowid


def pass_card(conn, system_id: int, game_id: str, team: str) -> None:
    conn.execute("INSERT OR IGNORE INTO passes VALUES (?,?,?,?)", (system_id, game_id, team, now_iso()))
    conn.commit()


def untail(conn, pick_id: int) -> None:
    conn.execute("DELETE FROM picks WHERE id=? AND result IS NULL", (pick_id,))
    conn.commit()


# ------------------------------------------------------------------ portfolio
def grade(bet: str, line, odds, pts: int, opp_pts: int) -> tuple[str, float]:
    if bet == "ats":
        x = pts - opp_pts + line
    elif bet == "ml":
        x = pts - opp_pts
    elif bet == "over":
        x = pts + opp_pts - line
    else:
        x = line - (pts + opp_pts)
    if x > 0:
        return "W", payout(odds)
    if x < 0:
        return "L", -1.0
    return "P", 0.0


def grade_picks(conn) -> int:
    n = 0
    rows = conn.execute(
        "SELECT p.*, t.pts, t.opp_pts FROM picks p JOIN team_games t ON t.game_id = p.game_id AND t.team = p.team "
        "WHERE p.result IS NULL AND t.won IS NOT NULL").fetchall()
    for p in rows:
        result, units = grade(p["bet"], p["line"], p["odds"], p["pts"], p["opp_pts"])
        conn.execute("UPDATE picks SET result=?, units=? WHERE id=?", (result, units * (p["stake"] or 1), p["id"]))
        n += 1
    conn.commit()
    return n


def portfolio(conn) -> dict:
    grade_picks(conn)
    rows = conn.execute(
        """SELECT p.*, s.name AS system, s.sport, t.opp, t.date, t.home, t.neutral, t.pts, t.opp_pts
           FROM picks p LEFT JOIN systems s ON s.id = p.system_id
           LEFT JOIN team_games t ON t.game_id = p.game_id AND t.team = p.team
           ORDER BY t.date DESC, p.id DESC""").fetchall()
    picks = []
    for r in rows:
        d = dict(r)
        d["label"] = describe_bet(r["bet"], {"team": r["team"], "line": r["line"], "total_line": r["line"],
                                             "ml": r["odds"], "spread_odds": r["odds"],
                                             f"{r['bet']}_odds": r["odds"]})["label"]
        picks.append(d)

    def summary(items):
        w = sum(1 for p in items if p["result"] == "W")
        l = sum(1 for p in items if p["result"] == "L")
        pu = sum(1 for p in items if p["result"] == "P")
        units = sum(p["units"] or 0 for p in items if p["result"])
        risked = sum(p["stake"] or 1 for p in items if p["result"] in ("W", "L"))
        return {"w": w, "l": l, "p": pu, "pending": sum(1 for p in items if not p["result"]),
                "units": round(units, 2), "roi": round(units / risked, 4) if risked else None,
                "pct": round(w / (w + l), 4) if w + l else None}

    by_system = {}
    for p in picks:
        by_system.setdefault(p["system"] or "(deleted system)", []).append(p)
    curve, cum = [], 0.0
    for p in sorted((p for p in picks if p["result"]), key=lambda p: (p["date"] or "", p["id"])):
        cum += p["units"] or 0
        curve.append([p["date"], round(cum, 2)])
    return {"total": summary(picks), "picks": picks, "curve": curve,
            "by_system": sorted(({"system": k, **summary(v)} for k, v in by_system.items()),
                                key=lambda x: -x["units"])}
