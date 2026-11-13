"""Which tournament a bookmaker's league is, for the court-speed factor.

Betano says "ATP - Tokio" or "WTA - Peking (Ž)", Chance "ATP Shanghai - tvrdý
p."; the history keys a tournament by the tour's own id, whose name is the city
("Tokyo", "BEIJING", "SUZHOU 125"). The league is cut down to its city, Czech
names put into English, and matched to the most recent tournament of that tour
with that name. No match means no venue: the factor stays neutral.
"""

import re

from .players import norm

CZECH = {"peking": "beijing", "tokio": "tokyo", "sanghaj": "shanghai", "soul": "seoul",
         "vuchan": "wuhan", "antverpy": "antwerp", "basilej": "basel", "viden": "vienna",
         "pariz": "paris", "hongkong": "hong kong", "rijad": "riyadh", "dauha": "doha",
         "dubaj": "dubai", "rim": "rome", "mnichov": "munich", "hamburk": "hamburg",
         "linec": "linz", "praha": "prague", "kanton": "guangzhou", "nanking": "nanjing"}
NOISE = {"atp", "wta", "masters", "kvalifikace", "tvrdy", "p", "antuka", "trava", "koberec",
         "z", "dvouhra", "muzi", "zeny"}


def _words(s: str) -> str:
    return " ".join(w for w in norm(re.sub(r"\(.*?\)", " ", s)).split() if w not in NOISE)


def city(league: str) -> str:
    """'WTA - Peking (Ž)' -> 'beijing', 'ATP Shanghai - tvrdý p.' -> 'shanghai',
    'WTA Guangzhou - tvrdý p. - K.Quevedo-Tagger' -> 'guangzhou'.

    Betano puts the tour first ('WTA - Peking'); Chance puts the city with the
    tour and may add the surface and the match after it - only the part that
    names the city is read, or the players' names would spoil the match."""
    parts = [p.strip() for p in league.split(" - ")]
    head = parts[1] if parts[0] in ("ATP", "WTA") and len(parts) > 1 else parts[0]
    s = _words(head)
    return CZECH.get(s, s)


class Venues:
    def __init__(self, conn, tour: str):
        self.tour, self.by_name, self.starts = tour, {}, {}
        with conn.cursor() as cur:
            cur.execute("""SELECT tournament_id, name, start_date FROM aces.tournament WHERE tour = %s
                           ORDER BY start_date""", (tour,))
            for tid, name, start in cur.fetchall():
                # 'SUZHOU 125' -> 'suzhou'; a later edition overwrites an earlier id.
                self.by_name[_words(name)] = tid
                if start:
                    self.starts.setdefault(tid, []).append(start)

    def in_season(self, league: str, kickoff) -> bool:
        """False when the league names a known tournament whose editions are
        all at another time of year: Chance listed a November exhibition as
        'WTA Indian Wells' (13 Nov 2026), and the model's best-of-three,
        tour-court pricing does not fit an exhibition. Unknown leagues pass."""
        tid = self.by_name.get(city(league))
        if not tid or not self.starts.get(tid):
            return True
        day = kickoff.timetuple().tm_yday
        return any(min(abs(day - s.timetuple().tm_yday), 366 - abs(day - s.timetuple().tm_yday)) <= 21
                   for s in self.starts[tid])

    def key(self, league: str) -> str | None:
        tid = self.by_name.get(city(league))
        return f"{self.tour}:{tid}" if tid else None
