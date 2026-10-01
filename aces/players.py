"""Turn a name as someone else spells it into a tour player id.

Two callers with different habits:
  price.py   a surname typed by hand, maybe with an initial: "Pliskova.Ka"
  odds.py    a bookmaker's full name, in either order: "Xinyu Gao", "Gao Xinyu"
"""

import re
import unicodedata

from . import model


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", s.lower().replace("_", " "))).strip()


class Players:
    def __init__(self, conn, tour: str = "WTA"):
        q = """SELECT id, name, max(played_at) AS last FROM (
                 SELECT player_a_id id, player_a name, played_at FROM aces.match WHERE tour = %(t)s
                 UNION ALL
                 SELECT player_b_id, player_b, played_at FROM aces.match WHERE tour = %(t)s) x
               GROUP BY id, name"""
        df = model.frame(conn, q, {"t": tour})
        alias = model.aliases(conn, tour)
        df["id"] = df["id"].map(lambda i: alias.get(i, i))
        self.df = df.sort_values("last", ascending=False).drop_duplicates("id")
        self.df["n"] = self.df["name"].map(norm)
        self.df["tokens"] = self.df["n"].map(lambda n: frozenset(n.split()))

    def by_surname(self, token: str) -> tuple[str, str]:
        """For hand-typed lines: an error when nothing or several match."""
        surname, _, first = token.partition(".")
        s, f = norm(surname), norm(first)
        hits = self.df[self.df.n.str.endswith(" " + s) | (self.df.n == s)]
        if f:
            hits = hits[hits.n.str.startswith(f)]
        if hits.empty:
            raise SystemExit(f"no player matches {token!r}")
        if len(hits) > 1:
            names = ", ".join(hits.name.head(5))
            raise SystemExit(f"{token!r} is ambiguous ({names}); add an initial, e.g. {surname}.X")
        h = hits.iloc[0]
        return h.id, h["name"]

    def by_full_name(self, name: str) -> str | None:
        """For a bookmaker's name: the id, or None rather than a guess.

        Same words in any order first, which settles "Gao Xinyu" against
        "Xinyu Gao". Then the surname plus a first name that starts the same
        way, for "Aliaksandra" against "Alexandra" style transliterations only
        when exactly one player fits.
        """
        toks = frozenset(norm(name).split())
        if not toks:
            return None
        exact = self.df[self.df.tokens == toks]
        if len(exact) == 1:
            return exact.iloc[0].id
        if len(exact) > 1:
            return None
        # One name inside the other: "Leylah Annie Fernandez" for "Leylah
        # Fernandez", "Maria Camila Osorio Serrano" for "Camila Osorio".
        inside = self.df[self.df.tokens.map(lambda t: len(t) >= 2 and (t < toks or toks < t))]
        if len(inside) == 1:
            return inside.iloc[0].id
        # Every word but one shared, and the odd ones agree on a first letter.
        def close(t):
            if len(t & toks) < max(1, min(len(t), len(toks)) - 1):
                return False
            a, b = sorted(t - toks), sorted(toks - t)
            return not a and not b or (len(a) == len(b) == 1 and a[0][0] == b[0][0])
        near = self.df[self.df.tokens.map(close)]
        return near.iloc[0].id if len(near) == 1 else None
