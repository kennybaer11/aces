#!/usr/bin/env python
"""Chance.cz ace and double-fault lines through webscraper.io, unattended.

    python chance_scrape.py                 collect, store and advise
    python chance_scrape.py --dry-run       show what would be collected
    python chance_scrape.py --hours 30      how far ahead to look

Replaces chance_collect.js, which had to run in an open browser tab because
Chance.cz refuses plain requests. webscraper.io renders the pages in its own
browser, so the same markets can be read from the page itself - no session, no
asking the site's internal API, and nothing for anyone to run by hand.

Two stages, like the football possession scraper:

  1. the tennis listing, one page, gives every match with its id, name and
     start time;
  2. the match pages of the ones worth reading give the markets.

Only matches whose players are both in our own ATP/WTA history are read: the
model cannot price anyone else, and that single rule keeps ITF and Challenger
matches - which carry no ace markets anyway - from costing a credit each.

The rows it writes are the ones load_chance.py already understands, so the
storing, pricing and advising stay in one place.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

log = logging.getLogger("chance_scrape")

API = "https://api.webscraper.io/api/v1"
LISTING_SITEMAP = int(os.getenv("WS_TENNIS_LISTING", "1546696"))
MATCH_SITEMAP = int(os.getenv("WS_TENNIS_MATCHES", "1546697"))
SITE = "https://www.chance.cz"
PRAGUE = ZoneInfo("Europe/Prague")
CACHE = Path(__file__).resolve().parent / "cache" / "chance"

# "Počet es v zápasu" and friends, in the order the page shows them.
MARKETS = {
    "počet es v zápasu": "aces",
    "počet es hráče v zápasu": "aces:p",
    "počet dvojchyb v zápasu": "df",
    "počet dvojchyb hráče v zápasu": "df:p",
}
# A line is always x.5; a price always has two decimals. Read together they
# arrive glued: "Méně než 22.51.88" is 22.5 at 1.88.
RUNG = re.compile(r"(Méně|Více) než (\d+\.5)(\d+\.\d\d)")

# Tour matches each player needs before a page is worth reading: the same bar
# the model uses before it will advise on a match (load_chance.py).
MIN_MATCHES = 10


def _token():
    token = os.getenv("WEBSCRAPER_TOKEN")
    if not token:
        raise SystemExit("WEBSCRAPER_TOKEN is not set.")
    return token


def start_job(sitemap_id, start_urls=None, page_load_delay=5000, request_interval=3000):
    body = {"sitemap_id": sitemap_id, "driver": "fulljs",
            "page_load_delay": page_load_delay, "request_interval": request_interval}
    if start_urls:
        body["start_urls"] = start_urls
    r = requests.post(f"{API}/scraping-job?api_token={_token()}", json=body, timeout=30)
    r.raise_for_status()
    return r.json()["data"]["id"]


def wait(job_id, minutes=40):
    """Poll until the job stops, or give up and say so."""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        s = requests.get(f"{API}/scraping-job/{job_id}?api_token={_token()}",
                         timeout=30).json()["data"]
        if s["status"] in ("finished", "failed", "shelved"):
            return s
        time.sleep(20)
    raise TimeoutError(f"job {job_id} still running after {minutes} min")


def rows_of(job_id):
    text = requests.get(f"{API}/scraping-job/{job_id}/json?api_token={_token()}",
                        timeout=120).text
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def browser_offset(listing):
    """How far webscraper.io's browser clock is from ours, to the half hour.

    Chance renders every time in the visitor's own timezone, and the scraping
    browser sits in US Eastern: a 16:30 Prague match came back as 10:30. The
    site prints that browser's clock in its header, so comparing it with the
    real time at the moment of the scrape puts every match time right, whatever
    region the scraper runs in.
    """
    stamps = [r.get("clock") for r in listing if (r.get("clock") or "").strip()]
    if not stamps:
        log.warning("no clock on the listing page - times taken as Prague's")
        return timedelta(0)
    hh, mm, _ss = (int(x) for x in stamps[0].strip().split(":"))
    now = datetime.now(PRAGUE)
    shown = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    gap = shown - now.replace(second=0, microsecond=0)
    # Wrap into -12..+12 hours: the clock has no date.
    hours = round(gap.total_seconds() / 1800) / 2
    hours = (hours + 12) % 24 - 12
    if hours:
        log.info("the scraping browser's clock is %+.1f h from Prague", hours)
    return timedelta(hours=hours)


def start_time(text, browser_now=None, offset=timedelta(0)):
    """'Dnes | 16:30', 'Zítra | 8:35', '17. 1. 2027 | 1:00' -> aware datetime.

    Everything on the page is in the scraping browser's own time, so "today"
    is its today: browser_now is our clock shifted by offset, and the result
    is shifted back. webscraper.io runs jobs in different regions - -6 h one
    minute and -10 h the next - so this is measured per job, never assumed.
    """
    if not text:
        return None
    now = browser_now or (datetime.now(PRAGUE) + offset)
    parts = [p.strip() for p in text.split("|")]
    clock = next((p for p in parts if re.fullmatch(r"\d{1,2}:\d{2}", p)), None)
    if not clock:
        return None
    hh, mm = (int(x) for x in clock.split(":"))
    day = next((p for p in parts if p not in (clock,)), "")
    if day.lower().startswith("dnes"):
        date = now.date()
    elif day.lower().startswith("zítra"):
        date = (now + timedelta(days=1)).date()
    else:
        m = re.match(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})?", day)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3) or now.year)
        date = datetime(y, mo, d).date()
    return datetime(date.year, date.month, date.day, hh, mm, tzinfo=PRAGUE) - offset


def competition(title):
    """'Kurzové sázky na ATP Lyon - tvrdý p. - Jacquet-Bran.Nakashima | Chance'
    -> 'ATP Lyon - tvrdý p.'"""
    if not title:
        return ""
    t = re.sub(r"^Kurzové sázky na\s*", "", title.split("|")[0].strip())
    # The players follow the surface; the surface always ends the tournament.
    m = re.match(r"(.+?(?:tvrdý p\.|antuka|tráva|koberec))\b", t)
    return (m.group(1) if m else t).strip(" -")


def parse_market(text):
    """One market element's text -> (market, [(box, line, over, under)])."""
    if not text:
        return None, []
    low = text.lower()
    key = next((k for k in sorted(MARKETS, key=len, reverse=True) if low.startswith(k)), None)
    if not key:
        return None, []
    market = MARKETS[key]
    body = text[len(key):]
    out, box = [], ""
    pos = 0
    pending = {}
    for m in RUNG.finditer(body):
        # Whatever stands between the last rung and this one names the player
        # the following rungs belong to ("Jacquet", "Bran.Nakashima").
        label = body[pos:m.start()].strip()
        if label:
            if pending:
                out.append((box, *pending.popitem()[1]))
            box = label
        pos = m.end()
        line = float(m.group(2))
        price = float(m.group(3))
        side = "over" if m.group(1) == "Více" else "under"
        cur = pending.setdefault(line, {"line": line})
        cur[side] = price
    rows = []
    for line, cur in pending.items():
        rows.append((box, line, cur.get("over"), cur.get("under")))
    # markets with one player per box keep their own box name
    return market, [(box if market.endswith(":p") else "", ln, o, u)
                    for (box, ln, o, u) in rows] if rows else []


# Prices are read again when the match is near, and otherwise only when what
# we hold has gone stale. Chance moves its ace lines little until the day.
RECHECK_HOURS = 6
NEAR_KICKOFF_HOURS = 4


def fresh_already(conn, event_id, kickoff, now):
    """True when we already hold lines for this match and need not pay again."""
    if not event_id or (kickoff - now) <= timedelta(hours=NEAR_KICKOFF_HOURS):
        return False
    with conn.cursor() as cur:
        cur.execute("""SELECT max(fetched_at) FROM aces.odds
                        WHERE source = 'chance' AND event_id = %s""", (event_id,))
        last = cur.fetchone()[0]
    return bool(last and last > datetime.now(timezone.utc) - timedelta(hours=RECHECK_HOURS))


def collect(hours, dry_run=False):
    from aces import db, model
    from aces.players import Players

    conn = db.connect()
    indexes = {tour: Players(conn, tour) for tour in ("ATP", "WTA")}
    # How many tour matches we hold for each player. A page is only worth a
    # credit if the model could advise on it, and that needs MIN_MATCHES each -
    # which also keeps Challenger and ITF matches out, since they carry no ace
    # markets at all.
    played = {}
    for tour in indexes:
        hist = model.load(conn, tour)
        played[tour] = (hist.player_a_id.value_counts()
                        .add(hist.player_b_id.value_counts(), fill_value=0)
                        if len(hist) else {})

    log.info("stage 1: the tennis listing")
    job = start_job(LISTING_SITEMAP)
    state = wait(job)
    listing = rows_of(job)
    log.info("listing job %s %s, %d row(s)", job, state["status"], len(listing))

    now = datetime.now(PRAGUE)
    offset = browser_offset(listing)
    wanted, skipped, skipped_fresh = [], 0, 0
    for r in listing:
        name = (r.get("match_name") or "").strip()
        href = (r.get("match_links-href") or r.get("match_links") or "").strip()
        when = start_time(r.get("datetime"), now + offset, offset)
        if not (name and href and when) or " - " not in name:
            continue
        if not (now < when <= now + timedelta(hours=hours)):
            continue
        home, away = (p.strip() for p in name.split(" - ", 1))
        tour = None
        for t, idx in indexes.items():
            a, b = idx.by_full_name(home), idx.by_full_name(away)
            if a and b and min(played[t].get(a, 0), played[t].get(b, 0)) >= MIN_MATCHES:
                tour = t
                break
        if not tour:
            skipped += 1
            continue
        if fresh_already(conn, str(r.get("match_id") or ""), when, now):
            skipped_fresh += 1
            continue
        wanted.append({"url": href if href.startswith("http") else SITE + href,
                       "name": name, "home": home, "away": away,
                       "start": when, "tour": tour,
                       "id": (r.get("match_id") or href.rstrip("/").split("/")[-1])})
    log.info("%d match(es) within %gh with both players in our data; %d other(s) skipped"
             "%s", len(wanted), hours, skipped,
             f", {skipped_fresh} already priced recently" if skipped_fresh else "")
    for w in wanted:
        log.info("   %s  %s  [%s]", w["start"].strftime("%d %b %H:%M"), w["name"], w["tour"])
    if dry_run or not wanted:
        return []

    log.info("stage 2: %d match page(s)", len(wanted))
    job = start_job(MATCH_SITEMAP, start_urls=[w["url"] for w in wanted])
    state = wait(job)
    pages = rows_of(job)
    log.info("match job %s %s, %d row(s)", job, state["status"], len(pages))

    # A page that rendered says so: it carries the tournament, both players and
    # the start time. One with no markets has simply not been priced yet -
    # Chance posts ace lines a few hours before play - and reading it again
    # would buy nothing. Only a page that did not render is worth a retry.
    rendered = {r.get("web_scraper_start_url") for r in pages
                if (r.get("teams") or "").strip() or (r.get("datum") or "").strip()}
    blank = [w for w in wanted if w["url"] not in rendered]
    if blank:
        log.info("retrying %d page(s) that did not render", len(blank))
        job = start_job(MATCH_SITEMAP, start_urls=[w["url"] for w in blank],
                        page_load_delay=8000)
        state = wait(job)
        extra = rows_of(job)
        log.info("retry job %s %s, %d row(s)", job, state["status"], len(extra))
        pages += extra

    by_url = {w["url"]: w for w in wanted}
    lines, pages_with_markets = [], set()
    for row in pages:
        w = by_url.get(row.get("web_scraper_start_url"))
        text = (row.get("market_text") or "").strip()
        if not (w and text):
            continue
        market, rungs = parse_market(text)
        if not market:
            continue
        pages_with_markets.add(w["url"])
        comp = competition(row.get("comp")) or w["tour"]
        for box, line, over, under in rungs:
            lines.append([w["id"], w["start"].isoformat(), comp, w["name"],
                          w["home"], w["away"], market, box, line, over, under])
    # The same market arrives once per price cell; one row each is enough.
    unique = {tuple(map(str, r[:9])): r for r in lines}
    log.info("%d line(s) from %d page(s) with markets", len(unique), len(pages_with_markets))
    return list(unique.values())


def main():
    ap = argparse.ArgumentParser(description="Chance.cz tennis lines via webscraper.io.")
    ap.add_argument("--hours", type=float, default=18,
                    help="how far ahead to read matches (default 18)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the matches that would be read, scrape no pages")
    ap.add_argument("--no-advise", action="store_true",
                    help="store and price, but advise nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    load_dotenv()

    rows = collect(args.hours, args.dry_run)
    if not rows:
        log.info("nothing to store.")
        return 0

    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{datetime.now(timezone.utc):%Y-%m-%d-%H%M}-ws.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    log.info("wrote %s", path)

    cmd = [sys.executable, str(Path(__file__).with_name("load_chance.py")), str(path)]
    if not args.no_advise:
        cmd.append("--advise")
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
