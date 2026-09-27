"""Projection model: power ratings -> projected spread/total/moneyline, edge and grade.

How it works (only information available before each game is used):

* Results ratings: weighted ridge regression of past game margins (and points for
  totals) on team strength, with exponential time decay. Old or scarce data is
  shrunk toward average automatically, which handles week 1.
* Market ratings: the same regression on past *closing lines*. It captures what
  the market believed about each team last week (injuries, QB changes...).
* Projection = blend of both (weight tuned on training seasons) plus a wind
  adjustment for totals fitted on history.
* Win/cover probabilities come from the empirical distribution of final margins
  for games with a similar closing spread, so key numbers (3, 7) are respected.
* Grade comes from expected value at the offered price.
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .report import payout

HALF_LIFE_DAYS = 120.0  # results weight halves every ~4 months
HISTORY_DAYS = 800      # ignore games older than this when fitting
RIDGE = 2.0
MARGIN_CAP = 24         # blowouts beyond this carry little extra signal
WIND_KNOT = 8.0         # mph where wind starts to matter for totals


# ---------------------------------------------------------------- linear algebra
def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small dense systems)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        if abs(pv) < 1e-12:
            continue
        inv = 1.0 / pv
        rowc = m[col]
        for r in range(col + 1, n):
            f = m[r][col] * inv
            if f:
                rowr = m[r]
                for c in range(col, n + 1):
                    rowr[c] -= f * rowc[c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r] if abs(m[r][r]) > 1e-12 else 0.0
    return x


def ridge_fit(obs: list[tuple[dict[int, float], float, float]], n_params: int,
              penalty: list[float]) -> list[float]:
    """obs: (sparse features {index: value}, target, weight). penalty per parameter."""
    ata = [[0.0] * n_params for _ in range(n_params)]
    atb = [0.0] * n_params
    for feats, y, w in obs:
        items = list(feats.items())
        for i, xi in items:
            wxi = w * xi
            atb[i] += wxi * y
            row = ata[i]
            for j, xj in items:
                row[j] += wxi * xj
    for i in range(n_params):
        ata[i][i] += penalty[i]
    return solve(ata, atb)


# ---------------------------------------------------------------- data
@dataclass
class Game:
    game_id: str
    season: int
    week: int
    day: date
    home: str
    away: str
    neutral: bool
    home_score: int | None
    away_score: int | None
    spread: float | None      # home favored positive
    total_line: float | None
    home_ml: int | None
    away_ml: int | None
    home_spread_odds: int | None
    away_spread_odds: int | None
    over_odds: int | None
    under_odds: int | None
    outdoors: bool
    wind: float | None
    extra: dict = field(default_factory=dict)

    @property
    def played(self) -> bool:
        return self.home_score is not None and self.away_score is not None

    @property
    def margin(self):
        return self.home_score - self.away_score if self.played else None

    @property
    def total(self):
        return self.home_score + self.away_score if self.played else None


def load_games(conn: sqlite3.Connection) -> list[Game]:
    fc = {}
    for r in conn.execute("SELECT game_id, wind_mph FROM weather_forecasts ORDER BY captured_at"):
        fc[r["game_id"]] = r["wind_mph"]
    out = []
    for r in conn.execute("SELECT * FROM games ORDER BY gameday, gametime, game_id"):
        if not r["gameday"]:
            continue
        outdoors = r["roof"] in ("outdoors", "open")
        wind = r["wind"]
        if wind is None and r["home_score"] is None:
            wind = fc.get(r["game_id"])  # forecast for games not played yet
        out.append(Game(
            game_id=r["game_id"], season=r["season"], week=r["week"],
            day=date.fromisoformat(r["gameday"]), home=r["home_team"], away=r["away_team"],
            neutral=r["location"] == "Neutral", home_score=r["home_score"], away_score=r["away_score"],
            spread=r["spread_line"], total_line=r["total_line"],
            home_ml=r["home_moneyline"], away_ml=r["away_moneyline"],
            home_spread_odds=r["home_spread_odds"], away_spread_odds=r["away_spread_odds"],
            over_odds=r["over_odds"], under_odds=r["under_odds"],
            outdoors=outdoors, wind=wind if outdoors else 0.0,
        ))
    return out


# ---------------------------------------------------------------- ratings
@dataclass
class Ratings:
    teams: dict[str, int]
    spread_res: list[float]   # [team strengths..., hfa]
    spread_mkt: list[float]
    pts_res: list[float]      # [off..., def..., mu, hfa]
    total_mkt: list[float]    # [team total tendency..., mu]

    def _t(self, team):
        return self.teams.get(team)

    def spread(self, which: str, home: str, away: str, neutral: bool) -> float:
        p = self.spread_res if which == "res" else self.spread_mkt
        h, a = self._t(home), self._t(away)
        s = (p[h] if h is not None else 0.0) - (p[a] if a is not None else 0.0)
        return s + (0.0 if neutral else p[-1])

    def total_res(self, home: str, away: str, neutral: bool) -> float:
        n = len(self.teams)
        p = self.pts_res
        mu, hfa = p[2 * n], p[2 * n + 1]
        h, a = self._t(home), self._t(away)
        off = lambda t: p[t] if t is not None else 0.0  # noqa: E731
        dfn = lambda t: p[n + t] if t is not None else 0.0  # noqa: E731
        home_pts = mu + off(h) + dfn(a) + (0 if neutral else hfa)
        away_pts = mu + off(a) + dfn(h)
        return home_pts + away_pts

    def total_mkt_pred(self, home: str, away: str) -> float:
        n = len(self.teams)
        p = self.total_mkt
        h, a = self._t(home), self._t(away)
        return p[n] + (p[h] if h is not None else 0.0) + (p[a] if a is not None else 0.0)


def fit_ratings(history: list[Game], as_of: date) -> Ratings:
    teams: dict[str, int] = {}
    for g in history:
        for t in (g.home, g.away):
            teams.setdefault(t, len(teams))
    n = len(teams)
    sp_res, sp_mkt, pts, tot_mkt = [], [], [], []
    for g in history:
        age = (as_of - g.day).days
        if age <= 0 or age > HISTORY_DAYS:
            continue
        w = 0.5 ** (age / HALF_LIFE_DAYS)
        h, a = teams[g.home], teams[g.away]
        hfa_x = 0.0 if g.neutral else 1.0
        if g.played:
            m = max(-MARGIN_CAP, min(MARGIN_CAP, g.margin))
            sp_res.append(({h: 1.0, a: -1.0, n: hfa_x}, m, w))
            pts.append(({h: 1.0, n + a: 1.0, 2 * n: 1.0, 2 * n + 1: hfa_x}, g.home_score, w))
            pts.append(({a: 1.0, n + h: 1.0, 2 * n: 1.0}, g.away_score, w))
        if g.spread is not None:
            # market lines are far less noisy than results: weight them more
            sp_mkt.append(({h: 1.0, a: -1.0, n: hfa_x}, g.spread, w * 4))
        if g.total_line is not None:
            tot_mkt.append(({h: 1.0, a: 1.0, n: 1.0}, g.total_line, w * 4))

    pen_sp = [RIDGE] * n + [0.05]
    pen_pts = [RIDGE] * (2 * n) + [1e-4, 0.05]
    pen_tot = [RIDGE] * n + [1e-4]
    return Ratings(
        teams=teams,
        spread_res=ridge_fit(sp_res, n + 1, pen_sp),
        spread_mkt=ridge_fit(sp_mkt, n + 1, pen_sp),
        pts_res=ridge_fit(pts, 2 * n + 2, pen_pts),
        total_mkt=ridge_fit(tot_mkt, n + 1, pen_tot),
    )


# ---------------------------------------------------------------- walk-forward raw projections
def raw_projections(games: list[Game], seasons: set[int] | None = None) -> dict[str, dict]:
    """For each game (in `seasons`), components predicted with data strictly before its week."""
    out: dict[str, dict] = {}
    by_slate: dict[tuple[int, int], list[Game]] = defaultdict(list)
    for g in games:
        if seasons is None or g.season in seasons:
            by_slate[(g.season, g.week)].append(g)
    for key in sorted(by_slate):
        slate = by_slate[key]
        as_of = min(g.day for g in slate)
        history = [g for g in games if g.day < as_of]
        if not history:
            continue
        rt = fit_ratings(history, as_of)
        for g in slate:
            out[g.game_id] = {
                "sp_res": rt.spread("res", g.home, g.away, g.neutral),
                "sp_mkt": rt.spread("mkt", g.home, g.away, g.neutral),
                "tot_res": rt.total_res(g.home, g.away, g.neutral),
                "tot_mkt": rt.total_mkt_pred(g.home, g.away),
            }
    return out


# ---------------------------------------------------------------- calibration
@dataclass
class Params:
    w_spread: float = 0.5     # weight on results ratings (rest on market ratings) -> raw model
    w_total: float = 0.5
    wind_coef_raw: float = -0.25  # pts per mph above WIND_KNOT when there is no market total
    k_spread: float = 0.0     # how much of (raw model - market line) survives vs results
    k_total: float = 0.0
    wind_coef_mkt: float = 0.0   # pts per mph above WIND_KNOT the market leaves on the table
    total_sd: float = 13.5
    train_seasons: list = field(default_factory=list)

    def as_dict(self):
        return self.__dict__.copy()


def _raw_spread(p: Params, r: dict) -> float:
    return p.w_spread * r["sp_res"] + (1 - p.w_spread) * r["sp_mkt"]


def _raw_total(p: Params, r: dict) -> float:
    return p.w_total * r["tot_res"] + (1 - p.w_total) * r["tot_mkt"]


def _lstsq(xs: list[list[float]], ys: list[float], ridge: float = 1e-6) -> list[float]:
    k = len(xs[0])
    ata = [[sum(x[i] * x[j] for x in xs) + (ridge if i == j else 0.0) for j in range(k)] for i in range(k)]
    atb = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(k)]
    return solve(ata, atb)


def calibrate(games: list[Game], raw: dict[str, dict], train_seasons: set[int]) -> Params:
    """Fit on training seasons only.

    1. Blend weights for the raw model (best predictor of results on its own).
    2. Shrinkage toward the market: regress (result - line) on (raw model - line).
       If the model adds nothing beyond the closing line, k ~ 0 and the grades stay
       near C: the model is honest about not knowing more than the market.
    3. Wind: the part of the total the market misses in windy outdoor games.
    """
    train = [g for g in games if g.season in train_seasons and g.played and g.game_id in raw]
    p = Params(train_seasons=sorted(train_seasons))
    best = None
    for w in [i / 20 for i in range(21)]:
        err = sum((g.margin - (w * raw[g.game_id]["sp_res"] + (1 - w) * raw[g.game_id]["sp_mkt"])) ** 2
                  for g in train)
        if best is None or err < best[0]:
            best = (err, w)
    p.w_spread = best[1]
    best = None
    for w in [i / 20 for i in range(21)]:
        err = sum((g.total - (w * raw[g.game_id]["tot_res"] + (1 - w) * raw[g.game_id]["tot_mkt"])) ** 2
                  for g in train)
        if best is None or err < best[0]:
            best = (err, w)
    p.w_total = best[1]

    def wind_x(g):
        return max(0.0, (g.wind or 0.0) - WIND_KNOT) if g.outdoors else 0.0

    # wind for the standalone model (no market total available)
    num = den = 0.0
    for g in train:
        x = wind_x(g)
        num += x * (g.total - _raw_total(p, raw[g.game_id]))
        den += x * x
    p.wind_coef_raw = num / den if den else 0.0

    sp = [g for g in train if g.spread is not None]
    if sp:
        xs = [[_raw_spread(p, raw[g.game_id]) - g.spread] for g in sp]
        ys = [g.margin - g.spread for g in sp]
        p.k_spread = max(0.0, min(1.0, _lstsq(xs, ys)[0]))
    tt = [g for g in train if g.total_line is not None]
    if tt:
        xs = [[_raw_total(p, raw[g.game_id]) - g.total_line, wind_x(g), 1.0] for g in tt]
        ys = [g.total - g.total_line for g in tt]
        k, wc, _bias = _lstsq(xs, ys)
        p.k_total = max(0.0, min(1.0, k))
        p.wind_coef_mkt = min(0.0, wc)
        res = [y - (p.k_total * x[0] + p.wind_coef_mkt * x[1]) for x, y in zip(xs, ys)]
        mean = sum(res) / len(res)
        p.total_sd = math.sqrt(sum((r - mean) ** 2 for r in res) / len(res))
    return p


# ---------------------------------------------------------------- probabilities
class MarginModel:
    """P(final home margin = m) around a projected spread.

    A smooth normal curve (sd ~12) centred on the projection, multiplied by
    key-number weights learned from history: margins of 3, 7, 10, 6, 14... happen
    far more often than a normal curve says. The weights are pooled over all games,
    so probabilities are stable and still respect key numbers.
    """

    SD = 12.0  # best out-of-sample log-likelihood on 2016+ with weights from 1999-2015
    RANGE = 60

    def __init__(self, games: list[Game], sd: float | None = None):
        if sd is not None:
            self.SD = sd
        expected = defaultdict(float)
        actual = defaultdict(float)
        for g in games:
            if not (g.played and g.spread is not None):
                continue
            actual[g.margin] += 1
            for m in range(-self.RANGE, self.RANGE + 1):
                expected[m] += self._mass(m, g.spread)
        self.weight = {}
        for m in range(-self.RANGE, self.RANGE + 1):
            self.weight[m] = (actual[m] + 2.0) / (expected[m] + 2.0) if expected else 1.0
        self._cache: dict[float, dict[int, float]] = {}

    def _mass(self, m: int, centre: float) -> float:
        return normal_cdf((m + 0.5 - centre) / self.SD) - normal_cdf((m - 0.5 - centre) / self.SD)

    def _weighted(self, centre: float) -> dict[int, float]:
        raw = {m: self._mass(m, centre) * self.weight[m] for m in range(-self.RANGE, self.RANGE + 1)}
        tot = sum(raw.values())
        return {m: v / tot for m, v in raw.items()}

    def dist(self, proj: float) -> dict[int, float]:
        """Distribution whose median sits on the projection.

        Median (not mean) is what a fair spread means: at a projection of 3.5 the
        favourite covers -3.5 half the time. Found by bisection on the centre.
        """
        key = round(proj, 2)
        if key not in self._cache:
            lo, hi = proj - 20.0, proj + 20.0
            for _ in range(30):
                mid = (lo + hi) / 2
                d = self._weighted(mid)
                # each integer margin m spread uniformly over [m-0.5, m+0.5] -> continuous median
                above = sum(v * min(1.0, max(0.0, m + 0.5 - proj)) for m, v in d.items())
                if above < 0.5:
                    lo = mid
                else:
                    hi = mid
            self._cache[key] = self._weighted((lo + hi) / 2)
        return self._cache[key]

    def cover_prob(self, proj: float, home_line: float) -> tuple[float, float]:
        """(P(home covers), P(push)) for a home spread like -3.5 (negative = home favored)."""
        d = self.dist(proj)
        win = sum(v for m, v in d.items() if m + home_line > 0)
        push = sum(v for m, v in d.items() if m + home_line == 0)
        return win, push

    def win_prob(self, proj: float) -> float:
        d = self.dist(proj)
        return sum(v for m, v in d.items() if m > 0) + 0.5 * d.get(0, 0.0)


def normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def ev(p_win: float, p_push: float, odds) -> float:
    """Expected profit per unit risked."""
    return p_win * payout(odds) - (1 - p_win - p_push)


def grade(e: float) -> str:
    # a fair coin flip at -110 is about -4.5% EV -> D; anything C or better has positive EV
    cuts = [(0.08, "A"), (0.06, "A-"), (0.045, "B+"), (0.03, "B"), (0.02, "B-"), (0.01, "C+"),
            (0.0, "C"), (-0.025, "D+"), (-0.05, "D")]
    for c, gname in cuts:
        if e >= c:
            return gname
    return "F"


def implied(odds: int) -> float:
    """Implied probability of American odds (includes the book's margin)."""
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def american(prob: float) -> int:
    prob = min(max(prob, 0.01), 0.99)
    return round(-100 * prob / (1 - prob)) if prob >= 0.5 else round(100 * (1 - prob) / prob)


def project_game(g: Game, raw: dict, p: Params, mm: MarginModel) -> dict:
    wind_x = max(0.0, (g.wind or 0.0) - WIND_KNOT) if g.outdoors else 0.0
    sp_raw = _raw_spread(p, raw)  # home favored positive
    tot_raw = _raw_total(p, raw)
    sp = g.spread + p.k_spread * (sp_raw - g.spread) if g.spread is not None else sp_raw
    if g.total_line is not None:
        tot = g.total_line + p.k_total * (tot_raw - g.total_line) + p.wind_coef_mkt * wind_x
    else:
        tot = tot_raw + p.wind_coef_raw * wind_x
    res = {
        "game_id": g.game_id, "season": g.season, "week": g.week, "date": g.day.isoformat(),
        "home": g.home, "away": g.away, "wind": g.wind if g.outdoors else None,
        "proj_spread_home": round(-sp, 1),   # displayed like a book: home -3.5
        "proj_total": round(tot, 1),
        "raw_spread_home": round(-sp_raw, 1),
        "raw_total": round(tot_raw + p.wind_coef_raw * wind_x, 1),
        "market_spread_home": None if g.spread is None else -g.spread,
        "market_total": g.total_line,
        "bets": [],
    }
    hwin = mm.win_prob(sp)
    if g.home_ml is not None and g.away_ml is not None:
        # anchor on the de-vigged market price; the margin model only supplies the
        # *change* in win probability implied by our spread vs the market spread
        imp_h, imp_a = implied(g.home_ml), implied(g.away_ml)
        mkt_h = imp_h / (imp_h + imp_a)
        base = mm.win_prob(g.spread) if g.spread is not None else hwin
        hwin = min(0.99, max(0.01, mkt_h + hwin - base))
    res["proj_ml_home"], res["proj_ml_away"] = american(hwin), american(1 - hwin)

    if g.spread is not None:
        home_line = -g.spread
        pc, pp = mm.cover_prob(sp, home_line)
        for side, prob, odds, line in (("home", pc, g.home_spread_odds, home_line),
                                       ("away", 1 - pc - pp, g.away_spread_odds, -home_line)):
            e = ev(prob, pp, odds)
            res["bets"].append({"market": "spread", "side": side, "line": line, "odds": odds or -110,
                                "prob": round(prob, 3), "ev": round(e, 4), "grade": grade(e),
                                "edge_pts": round((sp - g.spread) * (1 if side == "home" else -1), 1)})
    if g.total_line is not None:
        z = (g.total_line - tot) / p.total_sd
        p_under = normal_cdf(z)
        for side, prob, odds in (("over", 1 - p_under, g.over_odds), ("under", p_under, g.under_odds)):
            e = ev(prob, 0.0, odds)
            res["bets"].append({"market": "total", "side": side, "line": g.total_line, "odds": odds or -110,
                                "prob": round(prob, 3), "ev": round(e, 4), "grade": grade(e),
                                "edge_pts": round((tot - g.total_line) * (1 if side == "over" else -1), 1)})
    if g.home_ml is not None and g.away_ml is not None:
        for side, prob, odds in (("home", hwin, g.home_ml), ("away", 1 - hwin, g.away_ml)):
            e = ev(prob, 0.0, odds)
            res["bets"].append({"market": "ml", "side": side, "line": None, "odds": odds,
                                "prob": round(prob, 3), "ev": round(e, 4), "grade": grade(e),
                                "edge_pts": None})
    return res


def settle(g: Game, bet: dict):
    """True win / False loss / None push for a graded bet on a finished game."""
    if not g.played:
        return None
    if bet["market"] == "spread":
        m = g.margin if bet["side"] == "home" else -g.margin
        x = m + bet["line"]
    elif bet["market"] == "total":
        x = (g.total - bet["line"]) * (1 if bet["side"] == "over" else -1)
    else:
        x = g.margin if bet["side"] == "home" else -g.margin
    return None if x == 0 else x > 0
