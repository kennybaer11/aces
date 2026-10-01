#!/usr/bin/env python
"""Collect WTA singles matches with their serve statistics.

    python collect.py --years 2019-2026
    python collect.py --years 2026 --dry-run

Every finished match's stats are cached under ./cache, so a second run costs
nothing and a run that dies halfway resumes where it stopped. Matches already
stored with their stats are skipped.
"""

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

from aces import db, wta

log = logging.getLogger("collect")


def years(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def collect_year(conn, year: int, dry_run: bool) -> dict:
    counts = {"events": 0, "matches": 0, "no_stats": 0, "failed": 0}
    events = wta.season_tournaments(year)
    log.info("%d: %d tour-level events", year, len(events))
    started = time.monotonic()

    for ev in events:
        matches = wta.event_matches(ev)
        if not matches:
            continue
        counts["events"] += 1
        known = set() if dry_run else db.known_match_ids(conn, "WTA", ev["tournament_id"], year)
        if not dry_run:
            db.save_tournament(conn, ev)
        for m in matches:
            if m["match_id"] in known:
                continue
            try:
                serve = wta.match_serve(m) if m["score"] else []
            except Exception:                  # one bad match must not end the run
                log.exception("%s %s %s: stats failed", year, ev["name"], m["match_id"])
                counts["failed"] += 1
                continue
            if not serve:
                counts["no_stats"] += 1
            counts["matches"] += 1
            if not dry_run:
                db.save_match(conn, m, serve)
        if not dry_run:
            conn.commit()
        log.info("%d %-28s %3d matches  (%d so far, %.0fs)", year, ev["name"][:28],
                 len(matches), counts["matches"], time.monotonic() - started)
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2026")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    load_dotenv()
    conn = None if args.dry_run else db.connect()
    for y in years(args.years):
        c = collect_year(conn, y, args.dry_run)
        log.info("%d done: %s", y, c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
