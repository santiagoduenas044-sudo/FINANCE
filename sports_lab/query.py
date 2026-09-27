"""A small SDQL-like query language translated to safe, parameterised SQL.

Examples:
    total_line < 40 and wind >= 12 and outdoors
    dog and home and p_lost and p_margin <= -14
    team = PIT and line > 0 and rest > opp_rest
    team in (BUF, NE, NYJ) and temp <= 32
    money_pct >= 60 and bets_pct <= 40

Every row is one team in one game ("team perspective"), like SDQL.
Keywords: and/or/not (also y/o/no), in, is null / is not null.
"""
from __future__ import annotations

import re

# name -> (type, description in Spanish)
FIELDS: dict[str, tuple[str, str]] = {
    "season": ("num", "temporada (2025 = temporada 2025-26)"),
    "week": ("num", "semana"),
    "game_type": ("text", "REG, WC, DIV, CON, SB"),
    "playoff": ("num", "1 si es playoff"),
    "date": ("text", "fecha AAAA-MM-DD"),
    "weekday": ("text", "Sunday, Monday, Thursday..."),
    "hour": ("num", "hora de inicio (ET, 0-23)"),
    "team": ("text", "equipo (PIT, KC...)"),
    "opp": ("text", "rival"),
    "home": ("num", "1 si es local (no neutral)"),
    "neutral": ("num", "1 si es cancha neutral"),
    "div": ("num", "1 si es partido divisional"),
    "pts": ("num", "puntos del equipo"),
    "opp_pts": ("num", "puntos del rival"),
    "margin": ("num", "pts - opp_pts"),
    "total": ("num", "puntos totales del partido"),
    "line": ("num", "spread del equipo (negativo = favorito)"),
    "total_line": ("num", "over/under de cierre"),
    "ml": ("num", "moneyline del equipo"),
    "opp_ml": ("num", "moneyline del rival"),
    "fav": ("num", "1 si es favorito"),
    "dog": ("num", "1 si es underdog"),
    "won": ("num", "1 si ganó"),
    "lost": ("num", "1 si perdió"),
    "covered": ("num", "1 si cubrió el spread"),
    "ats_margin": ("num", "margin + line"),
    "ou_margin": ("num", "total - total_line"),
    "over": ("num", "1 si fue over"),
    "under": ("num", "1 si fue under"),
    "rest": ("num", "días de descanso"),
    "opp_rest": ("num", "días de descanso del rival"),
    "roof": ("text", "outdoors, dome, closed, open"),
    "surface": ("text", "grass, fieldturf..."),
    "outdoors": ("num", "1 si es al aire libre (outdoors u open)"),
    "temp": ("num", "temperatura (°F) registrada"),
    "wind": ("num", "viento (mph) registrado en el partido"),
    "wind_fcst": ("num", "viento pronosticado (mph) para partidos por jugar"),
    "coach": ("text", "head coach"),
    "opp_coach": ("text", "head coach rival"),
    "qb": ("text", "QB titular"),
    "opp_qb": ("text", "QB titular rival"),
    "game_no": ("num", "número de partido del equipo en la temporada"),
    "wins": ("num", "victorias antes del partido"),
    "losses": ("num", "derrotas antes del partido"),
    "ats_wins": ("num", "coberturas antes del partido"),
    "ats_losses": ("num", "no coberturas antes del partido"),
    "streak": ("num", "racha SU (+3 = 3 victorias seguidas, -2 = 2 derrotas)"),
    "ats_streak": ("num", "racha ATS"),
    "p_won": ("num", "ganó su partido anterior"),
    "p_lost": ("num", "perdió su partido anterior"),
    "p_covered": ("num", "cubrió en su partido anterior"),
    "p_margin": ("num", "margen del partido anterior"),
    "p_pts": ("num", "puntos anotados el partido anterior"),
    "p_opp_pts": ("num", "puntos permitidos el partido anterior"),
    "p_line": ("num", "spread del partido anterior"),
    "p_home": ("num", "fue local en el partido anterior"),
    "p_over": ("num", "el partido anterior fue over"),
    "p_ou_margin": ("num", "total - línea del partido anterior"),
    "p_ats_margin": ("num", "margen ATS del partido anterior"),
    "op_won": ("num", "el rival ganó su partido anterior"),
    "op_lost": ("num", "el rival perdió su partido anterior"),
    "op_covered": ("num", "el rival cubrió su partido anterior"),
    "op_margin": ("num", "margen del rival en su partido anterior"),
    "op_line": ("num", "spread del rival en su partido anterior"),
    "bets_pct": ("num", "% de tickets en el equipo (tus splits)"),
    "money_pct": ("num", "% de dinero en el equipo (tus splits)"),
    "ou_bets_over_pct": ("num", "% de tickets al over"),
    "ou_money_over_pct": ("num", "% de dinero al over"),
    "open_line": ("num", "spread de apertura (tus snapshots)"),
    "line_move": ("num", "line - open_line (negativo = la línea se movió hacia el equipo)"),
    "open_total": ("num", "total de apertura"),
    "total_move": ("num", "total_line - open_total"),
}

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>-?\d+(?:\.\d+)?)|(?P<str>'[^']*'|\"[^\"]*\")|(?P<op><=|>=|!=|<>|==|=|<|>|\+|-|\*|/)"
    r"|(?P<lp>\()|(?P<rp>\))|(?P<comma>,)|(?P<word>[A-Za-z_][A-Za-z0-9_.]*))"
)
_KEYWORDS = {"and": "and", "y": "and", "or": "or", "o": "or", "not": "not", "no": "not",
             "in": "in", "is": "is", "null": "null"}


class QueryError(ValueError):
    pass


def tokenize(text: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise QueryError(f"No entiendo el texto cerca de: {text[pos:pos + 15]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "word" and val.lower() in _KEYWORDS:
            kind, val = "kw", _KEYWORDS[val.lower()]
        elif kind == "str":
            val = val[1:-1]
        out.append((kind, val))
    return out


class _Parser:
    def __init__(self, tokens):
        self.t = tokens
        self.i = 0
        self.params: list = []

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
            raise QueryError(f"Se esperaba {val or kind} y llegó {v!r}")
        return v

    def parse(self) -> str:
        if not self.t:
            return "1=1"
        sql = self.or_expr()
        if self.i != len(self.t):
            raise QueryError(f"Sobra texto al final: {self.peek()[1]!r}")
        return sql

    def or_expr(self):
        parts = [self.and_expr()]
        while self.peek() == ("kw", "or"):
            self.take()
            parts.append(self.and_expr())
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    def and_expr(self):
        parts = [self.not_expr()]
        while self.peek() == ("kw", "and"):
            self.take()
            parts.append(self.not_expr())
        return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"

    def not_expr(self):
        if self.peek() == ("kw", "not"):
            self.take()
            return f"(NOT {self.not_expr()})"
        return self.comparison()

    def comparison(self):
        # parenthesised boolean group
        if self.peek()[0] == "lp" and self._is_bool_group():
            self.take()
            inner = self.or_expr()
            self.expect("rp")
            return inner
        left, lkind = self.arith()
        k, v = self.peek()
        if k == "op" and v in ("=", "==", "!=", "<>", "<", "<=", ">", ">="):
            self.take()
            right, rkind = self.arith(text_ctx=lkind == "text")
            if lkind == "word":
                raise QueryError(f"Campo desconocido: {left!r}")
            if rkind == "word":
                raise QueryError(f"Campo desconocido: {right!r}")
            op = {"==": "=", "<>": "!="}.get(v, v)
            collate = " COLLATE NOCASE" if "text" in (lkind, rkind) else ""
            return f"({left} {op} {right}{collate})"
        if (k, v) == ("kw", "in") or ((k, v) == ("kw", "not") and self.peek(1) == ("kw", "in")):
            negate = False
            if v == "not":
                self.take()
                negate = True
            self.take()
            self.expect("lp")
            items = []
            while True:
                item, ikind = self.arith(text_ctx=lkind == "text")
                if ikind == "word":
                    raise QueryError(f"Campo desconocido: {item!r}")
                items.append(item)
                if self.peek()[0] == "comma":
                    self.take()
                    continue
                self.expect("rp")
                break
            collate = " COLLATE NOCASE" if lkind == "text" else ""
            return f"({left}{collate} {'NOT IN' if negate else 'IN'} ({', '.join(items)}))"
        if (k, v) == ("kw", "is"):
            self.take()
            neg = False
            if self.peek() == ("kw", "not"):
                self.take()
                neg = True
            self.expect("kw", "null")
            return f"({left} IS {'NOT ' if neg else ''}NULL)"
        if lkind == "word":
            raise QueryError(f"Campo desconocido: {left!r}")
        # bare field used as boolean: `home`, `dog`, `p_lost`
        return f"({left} = 1)"

    def _is_bool_group(self) -> bool:
        depth = 0
        for j in range(self.i, len(self.t)):
            k, v = self.t[j]
            if k == "lp":
                depth += 1
            elif k == "rp":
                depth -= 1
                if depth == 0:
                    return False
            elif depth == 1 and (k == "kw" or (k == "op" and v in ("=", "==", "!=", "<>", "<", "<=", ">", ">="))):
                return True
        return False

    def arith(self, text_ctx=False):
        left, kind = self.term(text_ctx)
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            right, _ = self.term(text_ctx)
            left, kind = f"({left} {op} {right})", "num"
        return left, kind

    def term(self, text_ctx=False):
        left, kind = self.atom(text_ctx)
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            right, _ = self.atom(text_ctx)
            left, kind = f"({left} {op} {right})", "num"
        return left, kind

    def atom(self, text_ctx=False):
        k, v = self.take()
        if k == "num":
            self.params.append(float(v) if "." in v else int(v))
            return "?", "num"
        if k == "str":
            self.params.append(v)
            return "?", "lit"
        if k == "word":
            name = v.lower()
            # against a text field, a bare word is a value unless it names another text
            # field: `roof = outdoors` compares with the string, not the outdoors flag
            if name in FIELDS and not (text_ctx and FIELDS[name][0] != "text"):
                return name, FIELDS[name][0]
            if text_ctx:  # bare word compared with a text field, e.g. team = PIT
                self.params.append(v.upper() if v.isalpha() and len(v) <= 3 else v)
                return "?", "lit"
            return v, "word"
        if k == "lp":
            inner, kind = self.arith(text_ctx)
            self.expect("rp")
            return f"({inner})", kind
        if k == "op" and v == "-":
            inner, _ = self.atom(text_ctx)
            return f"(-{inner})", "num"
        raise QueryError(f"No esperaba {v!r}")


def to_sql(text: str) -> tuple[str, list]:
    """Translate a query to a SQL WHERE clause plus bound parameters."""
    p = _Parser(tokenize(text))
    where = p.parse()
    return where, p.params
