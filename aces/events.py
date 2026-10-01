"""What every bookmaker collector shares: one key per match across
bookmakers, and the model's probability for a rung."""

from .players import norm


def match_key(kickoff, name_1: str, name_2: str, id_1=None, id_2=None) -> str:
    """The same string for one match at every bookmaker.

    Tour ids when both players are known; otherwise each player's name words,
    sorted, so "Clara Tauson" and "Tauson Clara" agree. The players are sorted
    too, since bookmakers do not agree on who is listed first.
    """
    if id_1 and id_2:
        who = sorted([str(id_1), str(id_2)])
    else:
        who = sorted(" ".join(sorted(norm(n).split())) for n in (name_1, name_2))
    return f"{kickoff.date().isoformat()}|{who[0]}|{who[1]}"
