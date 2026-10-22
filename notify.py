#!/usr/bin/env python
"""Telegram messages for the tennis tips, the twin of the possession scraper's
notify.py - same bot, same chat, with a racket so the two are told apart.

    python notify.py             announce new tips and changed ones
    python notify.py --dry-run   print what would be sent, send nothing
    python notify.py --test      one message, to check the wiring

A tip is rewritten on every collection until its match starts, so the
announcement is only a snapshot: a tip can appear, change side, change stake,
or be withdrawn when the odds move. aces.advice remembers what Telegram was
last told (told_side, told_rating, told_at) and the second kind of message
reports the difference.

Nothing is marked as told until Telegram has accepted the message, so a failed
send is retried on the next run rather than lost.
"""

import argparse
import html
import logging
import os
import sys
from zoneinfo import ZoneInfo

import requests
from psycopg.rows import dict_row

log = logging.getLogger("notify")

PRAGUE = ZoneInfo("Europe/Prague")
TELEGRAM_LIMIT = 3500
SITE = "https://betken.cz/tennis"

# A stake change worth a message on its own; smaller moves are the model
# breathing, and reporting them every few hours would be noise.
RATING_STEP = 2

BOOKS = {"betano": "Betano", "chance": "Chance"}


def api(method, token, **params):
    """Telegram call. The token never reaches the log, only the method name."""
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                      data=params, timeout=30)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: "
                           f"{body.get('description') or r.status_code}")
    return body["result"]


def market_name(market):
    """aces -> match aces; aces:1 / aces:2 -> that player's aces."""
    stat, _, who = market.partition(":")
    label = {"aces": "aces", "df": "double faults"}.get(stat, stat)
    return label, who


def describe(r, changed=False):
    e = html.escape
    start = r["kickoff"].astimezone(PRAGUE)
    label, who = market_name(r["market"])
    about = {"1": r["player_1"], "2": r["player_2"]}.get(who)
    line = float(r["line"])
    # Betano quotes ladders as "8+", which is the over of 7.5.
    shown = (f"{line + 0.5:.0f}+" if r["source"] == "betano" and r["side"] == "over"
             else f"{line:.1f}")

    def tip(side, rating):
        if not side:
            return "no bet"
        out = f"<b>{side.upper()}</b> {shown} @ {float(r['odds']):.2f}"
        if rating:
            out += f" · stake {rating}/10"
        return out

    out = [
        f"<b>{e(r['player_1'])} v {e(r['player_2'])}</b>",
        f"{e(r['league'] or r['tour'] or '')} · {start:%a %d %b %H:%M} · "
        f"{e(about) + ' ' if about else ''}{label}",
    ]
    if changed:
        out.append(f"{tip(r['told_side'], r['told_rating'])} → {tip(r['side'], r['rating'])}")
    else:
        out.append(f"{tip(r['side'], r['rating'])} · edge {float(r['edge']):+.1%} · "
                   f"{BOOKS.get(r['source'], r['source'])}")
    return "\n".join(out)


def new_tips(conn):
    """Tips never announced, on matches still to start."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT * FROM aces.advice
             WHERE told_at IS NULL AND backed AND kickoff > now()
             ORDER BY kickoff, player_1, market""")
        return cur.fetchall()


def changed_tips(conn):
    """Tips whose advice has moved since Telegram was told about them.

    A withdrawn tip reads as "no bet": backed is false, and the side it was
    announced with is what the message compares against.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            SELECT * FROM aces.advice
             WHERE told_at IS NOT NULL
               AND kickoff > now()
               AND ((CASE WHEN backed THEN side END) IS DISTINCT FROM told_side
                    OR (backed AND abs(COALESCE(rating, 0)
                                       - COALESCE(told_rating, 0)) >= {RATING_STEP}))
             ORDER BY kickoff, player_1, market""")
        return cur.fetchall()


def build(rows, changed=False):
    if changed:
        header = (f"🔄🎾 <b>Tennis advice changed</b> - {len(rows)} tip"
                  f"{'' if len(rows) == 1 else 's'}")
        footer = ("\n\n<i>Tips are recomputed until the match starts; the record keeps "
                  f"the last version before it.</i>\n<a href=\"{SITE}\">Tennis tips</a>")
    else:
        header = (f"🎾 <b>New tennis tips</b> - {len(rows)} tip"
                  f"{'' if len(rows) == 1 else 's'}")
        footer = ("\n\n<i>Aces only, at +10% value or more against the model. Stakes stay "
                  f"capped while the record is short.</i>\n<a href=\"{SITE}\">Tennis tips</a>")
    messages, current = [], header
    for r in rows:
        block = "\n\n" + describe(r, changed)
        if len(current) + len(block) + len(footer) > TELEGRAM_LIMIT:
            messages.append(current + footer)
            current = header + " (cont.)"
        current += block
    messages.append(current + footer)
    return messages


def mark_told(conn, rows):
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("""UPDATE aces.advice
                              SET told_side = CASE WHEN backed THEN side END,
                                  told_rating = rating, told_at = now()
                            WHERE source = %s AND event_id = %s AND market = %s""",
                        (r["source"], r["event_id"], r["market"]))
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Telegram alerts for tennis tips.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    from dotenv import load_dotenv
    load_dotenv()
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")

    if args.test:
        if not (token and chat):
            raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
        api("sendMessage", token, chat_id=chat, text="🎾 Tennis tips are connected.")
        print("Test message sent.")
        return 0

    from aces import db
    conn = db.connect()
    fresh, moved = new_tips(conn), changed_tips(conn)
    if not (fresh or moved):
        log.info("No new tennis tip, nothing changed.")
        return 0

    messages = (build(fresh) if fresh else []) + (build(moved, changed=True) if moved else [])
    if args.dry_run or not (token and chat):
        if not args.dry_run:
            log.info("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - %d new and "
                     "%d changed tip(s) left unannounced.", len(fresh), len(moved))
        for m in messages:
            print(m, end="\n\n")
        return 0

    for m in messages:
        api("sendMessage", token, chat_id=chat, text=m, parse_mode="HTML",
            disable_web_page_preview="true")
    mark_told(conn, fresh + moved)
    log.info("Announced %d new and %d changed tip(s) in %d message(s).",
             len(fresh), len(moved), len(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
