"""Betano's own JSON (www.betano.cz/api/...), the feed behind its pages.

    /api/sport/tenis/                    every tennis region and its leagues
    /api/sport/tenis/{region}/{league}/  a league's matches, with start times
    /api/zapas-sance/{slug}/{id}/?bt=N   one match; N picks the market tab

The ace and double-fault markets come as ladders of "N+" rungs - at least N -
with an over price only. Market types are stable codes, so they are matched
on those rather than on the Czech names:

    5322 total aces       5324 / 5325 player 1 / 2 aces
    5323 total DFs        5326 / 5327 player 1 / 2 DFs

Read gently: a handful of requests a run, one per match, a second apart. If
Betano ever answers with a challenge page instead of JSON, the run stops and
says so; it does not try to get past it.
"""

import logging
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

BASE = "https://www.betano.cz"
GAP = 1.0
MARKETS = {"5322": "aces", "5324": "aces:1", "5325": "aces:2",
           "5323": "df", "5326": "df:1", "5327": "df:2"}

_s = requests.Session()
_s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 Chrome/128 Safari/537.36",
                   "Accept": "application/json"})
_last = 0.0


class Blocked(RuntimeError):
    """Betano answered with something other than its JSON."""


def _get(path: str) -> dict:
    global _last
    wait = GAP - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    r = _s.get(BASE + path, timeout=30)
    if r.status_code in (403, 429, 503) or "json" not in r.headers.get("content-type", ""):
        raise Blocked(f"{path}: HTTP {r.status_code}, {r.headers.get('content-type')}")
    r.raise_for_status()
    return r.json()["data"]


def wta_leagues() -> list[dict]:
    """Women's singles leagues: the WTA region, and the Slams' "(Ž)" draws."""
    out = []
    for group in _get("/api/sport/tenis/").get("regionGroups", []):
        for region in group.get("regions", []):
            for lg in region.get("leagues", []):
                name, url = lg.get("name", ""), lg.get("url", "")
                if "dvojice" in url or "Dvojice" in name:
                    continue
                women = "(Ž)" in name or url.rstrip("/").split("/")[-2].endswith("-z")
                if region.get("name") == "WTA" or (women and "itf" not in url):
                    out.append({"name": f"{region.get('name')} - {name}", "url": url})
    return out


def league_events(league: dict) -> list[dict]:
    """Matches still to start, as Betano lists them."""
    data = _get("/api" + league["url"])
    now = datetime.now(timezone.utc)
    out = []
    for block in data.get("blocks", []):
        for e in block.get("events", []):
            parts = e.get("participants") or []
            kickoff = datetime.fromtimestamp(e["startTime"] / 1000, timezone.utc)
            if e.get("liveNow") or len(parts) != 2 or kickoff <= now:
                continue
            if "/" in parts[0]["name"] or "/" in parts[1]["name"]:
                continue                                   # doubles that slipped in
            out.append({"event_id": str(e["id"]), "kickoff": kickoff,
                        "league": league["name"], "url": e["url"],
                        "name_1": parts[0]["name"], "name_2": parts[1]["name"]})
    return out


def event_ladders(event: dict) -> dict[str, list[tuple[int, float]]]:
    """{market: [(at_least, price), ...]} for the ace and DF markets on offer."""
    path = "/api" + event["url"]
    data = _get(path)
    found = _ladders(data.get("event", {}))
    if not found:
        # The landing tab is "popular"; the ladders live on the "all" tab,
        # whose number the response itself gives.
        tabs = data.get("markets") or data.get("nonBetBuilderTabs") or []
        want = [t["id"] for t in tabs if t.get("type") in ("aces", "doublefaults")]
        alls = [t["id"] for t in tabs if t.get("type") == "allmarkets"]
        for bt in (alls[:1] or want):
            found.update(_ladders(_get(f"{path}?bt={bt}").get("event", {})))
    return found


def _ladders(ev: dict) -> dict:
    out = {}
    for m in ev.get("markets", []):
        market = MARKETS.get(str(m.get("type")))
        if not market:
            continue
        rungs = []
        for s in m.get("selections", []):
            hit = re.fullmatch(r"\s*(\d+)\+\s*", s.get("name", ""))
            if hit and s.get("price"):
                rungs.append((int(hit.group(1)), float(s["price"])))
        if rungs:
            out[market] = sorted(rungs)
    return out
