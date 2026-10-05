"""Small daily messages for the Discord channel: a Bible verse in the morning
and a curious fact at midday. Not about trading decisions — just something
good to read between alerts.

Verses: Reina-Valera 1960 (short quotations, with their reference).
Facts: markets and finance first, and some general ones; only well-documented
things, phrased simply.

Each one comes in a fixed shuffled order by date, so a day always shows the
same verse (the !versiculo command and the morning message agree) and none
repeats until the list runs out.

Switches (.env): BOT_VERSICULOS=off / BOT_DATOS=off to turn them off;
BOT_VERSICULO_HORA (default 08:45) and BOT_DATO_HORA (default 12:30), ET.
"""
from __future__ import annotations

import json
import logging
import os
import random
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)
NY = "America/New_York"

# (text, reference) — Reina-Valera 1960
VERSES: list[tuple[str, str]] = [
    ("Todo lo puedo en Cristo que me fortalece.", "Filipenses 4:13"),
    ("Mira que te mando que te esfuerces y seas valiente; no temas ni desmayes, porque "
     "Jehová tu Dios estará contigo en dondequiera que vayas.", "Josué 1:9"),
    ("Fíate de Jehová de todo tu corazón, y no te apoyes en tu propia prudencia. "
     "Reconócelo en todos tus caminos, y él enderezará tus veredas.", "Proverbios 3:5-6"),
    ("Jehová es mi pastor; nada me faltará.", "Salmos 23:1"),
    ("Así que, no os afanéis por el día de mañana, porque el día de mañana traerá su afán. "
     "Basta a cada día su propio mal.", "Mateo 6:34"),
    ("Encomienda a Jehová tus obras, y tus pensamientos serán afirmados.", "Proverbios 16:3"),
    ("No temas, porque yo estoy contigo; no desmayes, porque yo soy tu Dios que te "
     "esfuerzo; siempre te ayudaré, siempre te sustentaré con la diestra de mi justicia.",
     "Isaías 41:10"),
    ("Porque yo sé los pensamientos que tengo acerca de vosotros, dice Jehová, "
     "pensamientos de paz, y no de mal, para daros el fin que esperáis.", "Jeremías 29:11"),
    ("Y sabemos que a los que aman a Dios, todas las cosas les ayudan a bien, esto es, a "
     "los que conforme a su propósito son llamados.", "Romanos 8:28"),
    ("Los pensamientos del diligente ciertamente tienden a la abundancia; mas todo el que "
     "se apresura alocadamente, de cierto va a la pobreza.", "Proverbios 21:5"),
    ("Las riquezas de vanidad disminuirán; pero el que recoge con mano laboriosa las "
     "aumenta.", "Proverbios 13:11"),
    ("Reparte a siete, y aun a ocho; porque no sabes el mal que vendrá sobre la tierra.",
     "Eclesiastés 11:2"),
    ("Porque ¿quién de vosotros, queriendo edificar una torre, no se sienta primero y "
     "calcula los gastos, a ver si tiene lo que necesita para acabarla?", "Lucas 14:28"),
    ("Sé diligente en conocer el estado de tus ovejas, y mira con cuidado por tus rebaños.",
     "Proverbios 27:23"),
    ("Porque donde esté vuestro tesoro, allí estará también vuestro corazón.", "Mateo 6:21"),
    ("Sean vuestras costumbres sin avaricia, contentos con lo que tenéis ahora; porque él "
     "dijo: No te desampararé, ni te dejaré.", "Hebreos 13:5"),
    ("Dios es nuestro amparo y fortaleza, nuestro pronto auxilio en las tribulaciones.",
     "Salmos 46:1"),
    ("Este es el día que hizo Jehová; nos gozaremos y alegraremos en él.", "Salmos 118:24"),
    ("Por la misericordia de Jehová no hemos sido consumidos, porque nunca decayeron sus "
     "misericordias. Nuevas son cada mañana; grande es tu fidelidad.", "Lamentaciones 3:22-23"),
    ("El corazón del hombre piensa su camino; mas Jehová endereza sus pasos.",
     "Proverbios 16:9"),
    ("Y si alguno de vosotros tiene falta de sabiduría, pídala a Dios, el cual da a todos "
     "abundantemente y sin reproche, y le será dada.", "Santiago 1:5"),
    ("No nos cansemos, pues, de hacer bien; porque a su tiempo segaremos, si no desmayamos.",
     "Gálatas 6:9"),
    ("Y todo lo que hagáis, hacedlo de corazón, como para el Señor y no para los hombres.",
     "Colosenses 3:23"),
    ("Por nada estéis afanosos, sino sean conocidas vuestras peticiones delante de Dios en "
     "toda oración y ruego, con acción de gracias.", "Filipenses 4:6"),
    ("Y la paz de Dios, que sobrepasa todo entendimiento, guardará vuestros corazones y "
     "vuestros pensamientos en Cristo Jesús.", "Filipenses 4:7"),
    ("Venid a mí todos los que estáis trabajados y cargados, y yo os haré descansar.",
     "Mateo 11:28"),
    ("La paz os dejo, mi paz os doy; yo no os la doy como el mundo la da. No se turbe "
     "vuestro corazón, ni tenga miedo.", "Juan 14:27"),
    ("Encomienda a Jehová tu camino, y confía en él; y él hará.", "Salmos 37:5"),
    ("Ve a la hormiga, oh perezoso, mira sus caminos, y sé sabio.", "Proverbios 6:6"),
    ("En toda labor hay fruto; mas las vanas palabras de los labios empobrecen.",
     "Proverbios 14:23"),
    ("Muchos pensamientos hay en el corazón del hombre; mas el consejo de Jehová permanecerá.",
     "Proverbios 19:21"),
    ("Pedid, y se os dará; buscad, y hallaréis; llamad, y se os abrirá.", "Mateo 7:7"),
    ("Porque no nos ha dado Dios espíritu de cobardía, sino de poder, de amor y de dominio "
     "propio.", "2 Timoteo 1:7"),
    ("Pero los que esperan a Jehová tendrán nuevas fuerzas; levantarán alas como las "
     "águilas; correrán, y no se cansarán; caminarán, y no se fatigarán.", "Isaías 40:31"),
    ("Alzaré mis ojos a los montes; ¿de dónde vendrá mi socorro? Mi socorro viene de "
     "Jehová, que hizo los cielos y la tierra.", "Salmos 121:1-2"),
    ("Gozosos en la esperanza; sufridos en la tribulación; constantes en la oración.",
     "Romanos 12:12"),
    ("Todas vuestras cosas sean hechas con amor.", "1 Corintios 16:14"),
    ("Sobre toda cosa guardada, guarda tu corazón; porque de él mana la vida.",
     "Proverbios 4:23"),
    ("Todo tiene su tiempo, y todo lo que se quiere debajo del cielo tiene su hora.",
     "Eclesiastés 3:1"),
    ("Los pensamientos son frustrados donde no hay consejo; mas en la multitud de "
     "consejeros se afirman.", "Proverbios 15:22"),
    ("El que es fiel en lo muy poco, también en lo más es fiel; y el que en lo muy poco es "
     "injusto, también en lo más es injusto.", "Lucas 16:10"),
    ("Así alumbre vuestra luz delante de los hombres, para que vean vuestras buenas obras, "
     "y glorifiquen a vuestro Padre que está en los cielos.", "Mateo 5:16"),
    ("Enséñanos de tal modo a contar nuestros días, que traigamos al corazón sabiduría.",
     "Salmos 90:12"),
    ("Cuando viene la soberbia, viene también la deshonra; mas con los humildes está la "
     "sabiduría.", "Proverbios 11:2"),
    ("Echa sobre Jehová tu carga, y él te sustentará; no dejará para siempre caído al justo.",
     "Salmos 55:22"),
    ("Echando toda vuestra ansiedad sobre él, porque él tiene cuidado de vosotros.",
     "1 Pedro 5:7"),
]

FACTS: list[str] = [
    # mercados y dinero
    "La Bolsa de Nueva York nació en 1792 con el Acuerdo de Buttonwood: 24 corredores lo "
    "firmaron bajo un árbol en Wall Street.",
    "Wall Street se llama así por un muro de madera que los holandeses levantaron en el "
    "siglo XVII en la antigua Nueva Ámsterdam.",
    "El Nasdaq abrió en 1971 como el primer mercado de acciones electrónico del mundo.",
    "El índice Nasdaq-100, el que sigue el NQ, se creó en 1985.",
    "Los contratos Micro E-mini, como el MNQ, se lanzaron en mayo de 2019 y valen la "
    "décima parte del contrato grande.",
    "Un tick del NQ es 0.25 puntos: $5 en un NQ y $0.50 en un MNQ.",
    "El 19 de octubre de 1987, el «Lunes Negro», el Dow Jones cayó 22.6 % en un solo día: "
    "su mayor caída porcentual diaria.",
    "El 6 de mayo de 2010 pasó el «Flash Crash»: el Dow cayó casi 1,000 puntos en minutos "
    "y recuperó buena parte esa misma tarde.",
    "La «tulipomanía» de Holanda (1636–1637) se considera una de las primeras burbujas "
    "especulativas de la historia.",
    "La Compañía Holandesa de las Indias Orientales, fundada en 1602, fue de las primeras "
    "en vender acciones al público.",
    "La explicación más popular de «toro» y «oso»: el toro ataca levantando los cuernos "
    "(mercado que sube) y el oso golpea hacia abajo con las garras (mercado que baja).",
    "Las velas japonesas vienen del Japón del siglo XVIII; se atribuyen a Munehisa Homma, "
    "un comerciante de arroz.",
    "Uno de los primeros mercados de futuros organizados fue el de arroz de Dojima, en "
    "Osaka, en el siglo XVIII.",
    "El CME de Chicago, donde se negocia el NQ, empezó en 1898 como la Chicago Butter and "
    "Egg Board: un mercado de mantequilla y huevos.",
    "El S&P 500 con sus 500 empresas existe desde 1957.",
    "El primer índice Dow Jones, de 1896, tenía solo 12 empresas.",
    "Warren Buffett compró su primera acción a los 11 años.",
    "La regla del 72: divide 72 entre el rendimiento anual y sabrás en cuántos años se "
    "duplica tu dinero. Al 10 % anual, unos 7 años.",
    "Isaac Newton perdió mucho dinero en la burbuja de la South Sea Company, en 1720.",
    "El mercado de divisas mueve más de 7 billones de dólares al día: es el mercado más "
    "grande del mundo.",
    "El 16 de marzo de 2020 el Dow cayó casi 3,000 puntos en un día, al inicio de la "
    "pandemia.",
    "Si el S&P 500 cae 7 % o 13 % en el día, la bolsa se detiene 15 minutos; si cae 20 %, "
    "cierra el resto del día. Se llaman «circuit breakers».",
    "Se cree que el símbolo $ viene de la abreviatura «ps» de los pesos españoles.",
    # generales
    "La miel casi no se echa a perder: se han encontrado tarros de miles de años en tumbas "
    "egipcias.",
    "El pulpo tiene tres corazones.",
    "La Torre Eiffel puede crecer unos 15 cm en verano porque el metal se dilata con el "
    "calor.",
    "Para la botánica, el plátano es una baya y la fresa no.",
    "La luz del Sol tarda unos 8 minutos y 20 segundos en llegar a la Tierra.",
    "En Venus, un día dura más que un año.",
    "El corazón humano late unas 100,000 veces al día.",
    "Los flamencos son rosados por lo que comen: algas y pequeños crustáceos con "
    "pigmentos.",
    "Las nutrias marinas se toman de las patas al dormir para que la corriente no las "
    "separe.",
    "Los tiburones existen desde antes que los árboles.",
    "Cleopatra vivió más cerca de la llegada del hombre a la Luna que de la construcción "
    "de la Gran Pirámide.",
    "Hay más partidas posibles de ajedrez que átomos en el universo observable.",
    "Los koalas duermen hasta 20 horas al día.",
    "El monte Everest crece unos milímetros cada año.",
]


def _order(n: int, seed: int) -> list[int]:
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    return idx


def _day_index(day) -> int:
    return int(pd.Timestamp(day).toordinal())


def verse_for(day) -> tuple[str, str]:
    """The verse of `day` (any date-like). Same day, same verse."""
    k = _day_index(day)
    order = _order(len(VERSES), seed=7 + k // len(VERSES))
    return VERSES[order[k % len(VERSES)]]


def fact_for(day) -> str:
    k = _day_index(day)
    order = _order(len(FACTS), seed=11 + k // len(FACTS))
    return FACTS[order[k % len(FACTS)]]


def verse_text(day) -> str:
    text, ref = verse_for(day)
    return f"*«{text}»*\n— **{ref}** (RVR1960)"


def fact_text(day) -> str:
    return fact_for(day)


def _on(env: str) -> bool:
    return os.environ.get(env, "on").strip().lower() not in ("off", "0", "no", "false")


def _hour(env: str, default: str) -> str:
    raw = os.environ.get(env, "").strip()
    try:
        return pd.Timestamp("2000-01-01 " + raw).strftime("%H:%M") if raw else default
    except ValueError:
        return default


class DailyNotes:
    """Sends the day's verse and fact once each, at their time (ET), and
    remembers what it sent (data_store/inspiration.json) so a restart never
    repeats them."""

    def __init__(self, state_file: Path | None):
        self.state_file = Path(state_file) if state_file else None
        self.sent: dict[str, str] = {}
        if self.state_file and self.state_file.exists():
            try:
                self.sent = json.loads(self.state_file.read_text())
            except (OSError, ValueError):
                self.sent = {}

    def _save(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self.sent))
        except OSError as exc:
            log.warning("inspiration state not saved (%s)", type(exc).__name__)

    def due(self, now) -> list[tuple[str, str, str]]:
        """[(kind, title, text)] to send now; each marked as sent."""
        t = pd.Timestamp(now)
        t = t.tz_localize(NY) if t.tzinfo is None else t.tz_convert(NY)
        day, hm = t.date().isoformat(), t.strftime("%H:%M")
        out = []
        plan = (("verse", "BOT_VERSICULOS", _hour("BOT_VERSICULO_HORA", "08:45"),
                 "📖 Palabra del día", verse_text),
                ("fact", "BOT_DATOS", _hour("BOT_DATO_HORA", "12:30"),
                 "💡 Dato curioso", fact_text))
        for kind, env, at, title, make in plan:
            # within 3 hours after its time: a bot that starts at noon still
            # sends the morning verse, one started at night does not
            late = (pd.Timestamp("2000-01-01 " + hm) - pd.Timestamp("2000-01-01 " + at))
            if not _on(env) or self.sent.get(kind) == day or not (
                    pd.Timedelta(0) <= late <= pd.Timedelta(hours=3)):
                continue
            self.sent[kind] = day
            out.append((kind, title, make(t.date())))
        if out:
            self._save()
        return out
