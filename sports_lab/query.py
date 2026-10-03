"""A KillerSports/SDQL-style query language translated to safe, parameterised SQL.

Each row is one team in one game ("team perspective"), like SDQL. Examples:

    total < 40 and wind >= 12 and outdoors
    team = PIT and p:ATSW and H and season > 2020
    D and A and p:L and p:margin <= -14
    rest > o:rest + 3 and F
    team in (BUF, NE, NYJ) and temp <= 32
    money_pct >= 60 and bets_pct <= 40

Prefixes (SDQL style):
    p:   the team's previous game this season       (p:W, p:ATSW, p:points)
    pp:  two games ago
    o:   the opponent in this game                  (o:rest, o:rank, o:team)
    op:  the opponent's previous game               (op:L, op:margin)

Shorthands: H home, A away, N neutral, F favorite, D underdog, W win, L loss,
ATSW / ATSL / ATSP cover / no cover / push, O over, U under.
Keywords: and / or / not, in (...), is null, is not null.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# query name -> (SQL template on alias {a}, type, description)
FIELDS: dict[str, tuple[str, str, str]] = {
    "sport": ("{a}.sport", "text", "sport: nfl, cfb, nba, cbb, nhl, mlb"),
    "season": ("{a}.season", "num", "season"),
    "week": ("{a}.week", "num", "week (football)"),
    "month": ("{a}.month", "num", "month (1-12)"),
    "date": ("CAST(REPLACE({a}.date, '-', '') AS INTEGER)", "num", "date as YYYYMMDD (date >= 20250901)"),
    "day": ("{a}.weekday", "text", "day of week: Sunday, Monday, Thursday..."),
    "hour": ("{a}.hour", "num", "kickoff hour (ET, 0-23)"),
    "game_type": ("{a}.game_type", "text", "REG or playoff round (WC, DIV, CON, SB, POST)"),
    "playoff": ("{a}.playoff", "num", "playoff game"),
    "team": ("{a}.team", "text", "team code (PIT, KC, UGA...)"),
    "name": ("{a}.team_name", "text", "full team name (when available)"),
    "site": ("CASE WHEN {a}.neutral = 1 THEN 'neutral' WHEN {a}.home = 1 THEN 'home' ELSE 'away' END",
             "text", "home, away or neutral"),
    "home": ("{a}.home", "num", "home team (shorthand H)"),
    "away": ("(1 - {a}.home - {a}.neutral)", "num", "away team (shorthand A)"),
    "neutral": ("{a}.neutral", "num", "neutral site (shorthand N)"),
    "div": ("{a}.div", "num", "division game (NFL)"),
    "rank": ("{a}.rank", "num", "AP rank (1-25, college)"),
    "ranked": ("({a}.rank IS NOT NULL)", "num", "has an AP rank"),
    "points": ("{a}.pts", "num", "points / runs / goals scored"),
    "margin": ("{a}.margin", "num", "points - o:points"),
    "game_total": ("{a}.total", "num", "actual combined score"),
    "line": ("{a}.line", "num", "team spread (negative = favorite)"),
    "total": ("{a}.total_line", "num", "closing over/under (as in SDQL)"),
    "ml": ("{a}.ml", "num", "team moneyline"),
    "fav": ("{a}.fav", "num", "favorite (shorthand F)"),
    "dog": ("{a}.dog", "num", "underdog (shorthand D)"),
    "won": ("{a}.won", "num", "won (shorthand W)"),
    "lost": ("{a}.lost", "num", "lost (shorthand L)"),
    "tie": ("{a}.tie", "num", "tie"),
    "covered": ("{a}.covered", "num", "covered the spread (shorthand ATSW)"),
    "ats_lost": ("({a}.covered = 0)", "num", "failed to cover (shorthand ATSL)"),
    "ats_push": ("{a}.ats_push", "num", "ATS push (shorthand ATSP)"),
    "ats_margin": ("{a}.ats_margin", "num", "margin + line"),
    "ou_margin": ("{a}.ou_margin", "num", "game_total - total"),
    "over": ("{a}.over", "num", "went over (shorthand O)"),
    "under": ("{a}.under", "num", "went under (shorthand U)"),
    "rest": ("{a}.rest", "num", "days of rest"),
    "roof": ("{a}.roof", "text", "outdoors, dome, closed, open"),
    "surface": ("{a}.surface", "text", "grass, fieldturf..."),
    "outdoors": ("{a}.outdoors", "num", "played outdoors"),
    "temp": ("{a}.temp", "num", "temperature (°F)"),
    "wind": ("{a}.wind", "num", "actual game wind (mph, NFL)"),
    "wind_fcst": ("{a}.wind_fcst", "num", "forecast wind for upcoming games (mph)"),
    "coach": ("{a}.coach", "text", "head coach (NFL)"),
    "qb": ("{a}.qb", "text", "starting QB (NFL)"),
    "game_no": ("{a}.game_no", "num", "team's game number this season"),
    "wins": ("{a}.wins", "num", "wins before the game"),
    "losses": ("{a}.losses", "num", "losses before the game"),
    "ats_wins": ("{a}.ats_wins", "num", "ATS wins before the game"),
    "ats_losses": ("{a}.ats_losses", "num", "ATS losses before the game"),
    "streak": ("{a}.streak", "num", "SU streak (+3 = won 3 straight, -2 = lost 2)"),
    "ats_streak": ("{a}.ats_streak", "num", "ATS streak"),
    "bets_pct": ("{a}.bets_pct", "num", "% of tickets on the team (your splits)"),
    "money_pct": ("{a}.money_pct", "num", "% of money on the team (your splits)"),
    "over_bets_pct": ("{a}.ou_bets_over_pct", "num", "% of tickets on the over"),
    "over_money_pct": ("{a}.ou_money_over_pct", "num", "% of money on the over"),
    "open_line": ("{a}.open_line", "num", "opening spread (your snapshots)"),
    "line_move": ("{a}.line_move", "num", "line - open_line (negative = moved toward the team)"),
    "open_total": ("{a}.open_total", "num", "opening total"),
    "total_move": ("{a}.total_move", "num", "total - open_total"),
}

ALIASES = {
    "h": "home", "a": "away", "n": "neutral", "f": "fav", "d": "dog", "favorite": "fav", "underdog": "dog",
    "w": "won", "l": "lost", "t": "tie", "win": "won", "loss": "lost",
    "atsw": "covered", "atsl": "ats_lost", "atsp": "ats_push", "o": "over", "u": "under",
    "pts": "points", "total_line": "total", "ou_line": "total", "weekday": "day", "opp": "o:team",
    "opp_rest": "o:rest", "opp_pts": "o:points", "opp_rank": "o:rank", "opp_coach": "o:coach",
    "opp_qb": "o:qb", "opp_ml": "o:ml",
}

# prefix -> (table alias, join SQL)
JOINS = {
    "p": ("p", "LEFT JOIN team_games p ON p.sport = t.sport AND p.team = t.team AND p.season = t.season "
               "AND p.game_no = t.game_no - 1"),
    "pp": ("pp", "LEFT JOIN team_games pp ON pp.sport = t.sport AND pp.team = t.team AND pp.season = t.season "
                 "AND pp.game_no = t.game_no - 2"),
    "o": ("o", "LEFT JOIN team_games o ON o.game_id = t.game_id AND o.team = t.opp"),
    # needs the `o` join, added automatically
    "op": ("op", "LEFT JOIN team_games op ON op.sport = o.sport AND op.team = o.team AND op.season = o.season "
                 "AND op.game_no = o.game_no - 1"),
}
_PREFIX_CHAIN = {("p", "o"): "op", ("o", "p"): "op"}

_CMP = ("=", "==", "!=", "<>", "<", "<=", ">", ">=")
_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<str>'[^']*'|\"[^\"]*\")|(?P<op><=|>=|!=|<>|==|=|<|>|\+|-|\*|/)"
    r"|(?P<lp>\()|(?P<rp>\))|(?P<comma>,)|(?P<word>(?:[A-Za-z]{1,2}:)*[A-Za-z_][A-Za-z0-9_]*))"
)
# O is the over shorthand and NO is New Orleans, so they are never keywords
_KEYWORDS = {"and": "and", "or": "or", "not": "not",
             "in": "in", "is": "is", "null": "null"}


class QueryError(ValueError):
    pass


@dataclass
class Compiled:
    where: str
    params: list
    joins: list[str] = field(default_factory=list)
    interpreted: str = ""


def tokenize(text: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise QueryError(f"Can't parse the query near: {text[pos:pos + 15]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "word" and val.lower() in _KEYWORDS:
            kind, val = "kw", _KEYWORDS[val.lower()]
        elif kind == "str":
            val = val[1:-1]
        out.append((kind, val))
    return out


def resolve(word: str) -> tuple[str, str, str, str | None] | None:
    """'p:ATSW' -> (sql, type, canonical name, join prefix); None if unknown."""
    low = word.lower()
    # older spelling p_margin / op_won / pp_covered -> p:margin / op:won / pp:covered
    m = re.match(r"^(op|pp|p)_(\w+)$", low)
    if m and (m.group(2) in FIELDS or m.group(2) in ALIASES):
        low = f"{m.group(1)}:{m.group(2)}"
    parts = low.split(":")
    prefixes, name = parts[:-1], parts[-1]
    name = ALIASES.get(name, name)
    if ":" in name:  # alias that carries its own prefix, e.g. opp -> o:team
        extra, name = name.split(":")
        prefixes = prefixes + [extra]
    if name not in FIELDS:
        return None
    if len(prefixes) > 2:
        return None
    if len(prefixes) == 2:
        combo = _PREFIX_CHAIN.get(tuple(prefixes))
        if combo is None:
            return None
        prefixes = [combo]
    prefix = prefixes[0] if prefixes else None
    if prefix is not None and prefix not in JOINS:
        return None
    tmpl, typ, _ = FIELDS[name]
    alias = JOINS[prefix][0] if prefix else "t"
    canonical = f"{prefix}:{name}" if prefix else name
    return tmpl.format(a=alias), typ, canonical, prefix


class _Parser:
    """Recursive descent; every node returns (sql, kind, pretty)."""

    def __init__(self, tokens):
        self.t = tokens
        self.i = 0
        self.params: list = []
        self.joins: set[str] = set()

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, kind, val=None):
        k, v = self.take()
        if k != kind or (val is not None and v != val):
            raise QueryError(f"Expected {val or kind} but got {v!r}")
        return v

    def parse(self) -> tuple[str, str]:
        if not self.t:
            return "1=1", ""
        sql, pretty = self.or_expr()
        if self.i != len(self.t):
            raise QueryError(f"Unexpected text at the end: {self.peek()[1]!r}")
        return sql, pretty

    def or_expr(self):
        parts = [self.and_expr()]
        while self.peek() == ("kw", "or"):
            self.take()
            parts.append(self.and_expr())
        if len(parts) == 1:
            return parts[0]
        return "(" + " OR ".join(p[0] for p in parts) + ")", " or ".join(p[1] for p in parts)

    def and_expr(self):
        parts = [self.not_expr()]
        while self.peek() == ("kw", "and"):
            self.take()
            parts.append(self.not_expr())
        if len(parts) == 1:
            return parts[0]
        return "(" + " AND ".join(p[0] for p in parts) + ")", " and ".join(p[1] for p in parts)

    def not_expr(self):
        if self.peek() == ("kw", "not"):
            self.take()
            sql, pretty = self.not_expr()
            return f"(NOT {sql})", f"not {pretty}"
        return self.comparison()

    def comparison(self):
        if self.peek()[0] == "lp" and self._is_bool_group():
            self.take()
            sql, pretty = self.or_expr()
            self.expect("rp")
            return sql, f"({pretty})"
        left, lkind, lp = self.arith()
        k, v = self.peek()
        if k == "op" and v in _CMP:
            self.take()
            right, rkind, rp = self.arith(text_ctx=lp if lkind == "text" else False)
            for side, kind in ((lp, lkind), (rp, rkind)):
                if kind == "word":
                    raise QueryError(f"Unknown field: {side!r}")
            op = {"==": "=", "<>": "!="}.get(v, v)
            collate = " COLLATE NOCASE" if "text" in (lkind, rkind) else ""
            return f"({left} {op} {right}{collate})", f"{lp} {op} {rp}"
        if (k, v) == ("kw", "in") or ((k, v) == ("kw", "not") and self.peek(1) == ("kw", "in")):
            negate = v == "not"
            if negate:
                self.take()
            self.take()
            self.expect("lp")
            items, pitems = [], []
            while True:
                item, ikind, ip = self.arith(text_ctx=lp if lkind == "text" else False)
                if ikind == "word":
                    raise QueryError(f"Unknown field: {ip!r}")
                items.append(item)
                pitems.append(ip)
                if self.peek()[0] == "comma":
                    self.take()
                    continue
                self.expect("rp")
                break
            collate = " COLLATE NOCASE" if lkind == "text" else ""
            kw = "NOT IN" if negate else "IN"
            return f"({left}{collate} {kw} ({', '.join(items)}))", f"{lp} {kw.lower()} ({', '.join(pitems)})"
        if (k, v) == ("kw", "is"):
            self.take()
            neg = False
            if self.peek() == ("kw", "not"):
                self.take()
                neg = True
            self.expect("kw", "null")
            return f"({left} IS {'NOT ' if neg else ''}NULL)", f"{lp} is {'not ' if neg else ''}null"
        if lkind == "word":
            raise QueryError(f"Unknown field: {lp!r}")
        if lkind == "text":
            raise QueryError(f"{lp} needs a comparison, e.g. {lp} = PIT")
        return f"({left} = 1)", lp  # bare field as boolean: H, D, p:L

    def _is_bool_group(self) -> bool:
        depth = 0
        for j in range(self.i, len(self.t)):
            k, v = self.t[j]
            if k == "lp":
                depth += 1
            elif k == "rp":
                depth -= 1
                if depth == 0:
                    # `(H)` or `(p:W)` alone is a boolean group too
                    return j == self.i + 2 and self.t[self.i + 1][0] == "word"
            elif depth == 1 and (k == "kw" or (k == "op" and v in _CMP)):
                return True
        return False

    def arith(self, text_ctx=False):
        left, kind, pretty = self.term(text_ctx)
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            right, _, rp = self.term(text_ctx)
            left, kind, pretty = f"({left} {op} {right})", "num", f"{pretty} {op} {rp}"
        return left, kind, pretty

    def term(self, text_ctx=False):
        left, kind, pretty = self.atom(text_ctx)
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            right, _, rp = self.atom(text_ctx)
            left, kind, pretty = f"({left} {op} {right})", "num", f"{pretty} {op} {rp}"
        return left, kind, pretty

    def atom(self, text_ctx=False):
        k, v = self.take()
        if k == "num":
            self.params.append(float(v) if "." in v else int(v))
            return "?", "num", v
        if k == "str":
            self.params.append(v)
            return "?", "lit", f"'{v}'"
        if k == "word":
            res = resolve(v)
            # against a text field a bare word is a value unless it names another text field:
            # `roof = outdoors` compares with the string, `team = o:team` with the column
            if res is not None and not (text_ctx and res[1] != "text"):
                sql, typ, canonical, prefix = res
                if prefix:
                    self.joins.add(prefix)
                return sql, typ, canonical
            if text_ctx:  # text_ctx = canonical name of the text field being compared
                val = v.upper() if str(text_ctx).endswith("team") else v
                self.params.append(val)
                return "?", "lit", f"'{val}'"
            return v, "word", v
        if k == "lp":
            inner, kind, pretty = self.arith(text_ctx)
            self.expect("rp")
            return f"({inner})", kind, f"({pretty})"
        if k == "op" and v == "-":
            inner, _, pretty = self.atom(text_ctx)
            return f"(-{inner})", "num", f"-{pretty}"
        if k is None:
            raise QueryError("The query ends too early")
        raise QueryError(f"Unexpected {v!r}")


def compile_query(text: str) -> Compiled:
    p = _Parser(tokenize(text))
    where, pretty = p.parse()
    if "op" in p.joins:
        p.joins.add("o")
    joins = [JOINS[k][1] for k in ("p", "pp", "o", "op") if k in p.joins]
    return Compiled(where=where, params=p.params, joins=joins, interpreted=pretty)


def to_sql(text: str) -> tuple[str, list]:
    """WHERE clause (on alias t) plus bound parameters."""
    c = compile_query(text)
    return c.where, c.params
