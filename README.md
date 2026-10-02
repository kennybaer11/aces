# aces — pricing ace and double-fault lines on the WTA tour

The tennis counterpart to posession's possession model: estimate how many aces
and double faults each player will hit, turn that into a probability for any
over/under line, and compare it with Chance.cz's prices.

Separate from posession, but shares its Neon database through its own `aces`
schema, so `DATABASE_URL` is the same string.

## Data

The WTA's own API (`api.wtatennis.com`), the feed behind wtatennis.com. It has
serve statistics for every singles match — aces, double faults, serve points,
first serves, service games, break points — for the match and for each set,
for Grand Slams, WTA 1000/500/250 and WTA 125 events back to at least 2016.

Not covered:

- **ATP.** Sackmann's free `tennis_atp` repo is gone (404) and atptour.com sits
  behind a Cloudflare bot check. Adding the men needs a paid feed.
- **ITF.** The events are in the WTA calendar but have no matches in the API.

```bash
pip install -r requirements.txt
python collect.py --years 2016-2026     # a few hours the first time, cached after
python collect.py --years 2026          # refresh the current season
```

## The model

`aces/model.py`, three parts, each estimated only from earlier matches:

1. **Rates per serve point:** for aces, double faults and serve points won,
   each player's rate relative to an average player in the same matches (same
   surface, same opponents). Time-weighted (one-year half-life), with a separate
   surface-specific figure that is shrunk towards the player's overall one. The
   returner's side is estimated the same way, because some players get aced far
   more than others.
2. **Match length:** `aces/sim.py` simulates matches point by point on a grid
   of serve-point win probabilities. That gives the joint distribution of how
   many points each player serves. A lopsided match is short, and a short match
   has few aces.
3. **Counts:** a negative binomial given the serve points, mixed over the
   simulated matches. Totals are convolved within each simulated match, so the
   match length the two players share is kept.

```bash
python backtest.py --start 2025-01-01   # walk-forward, against a naive baseline
```

## Pricing lines

Copy `lines.example.txt` to `lines.txt` and paste in the Chance.cz lines.

```bash
python price.py lines.txt               # model probability, fair odds, EV; stores the lines
python price.py --settle                # after collect.py: results and running profit
```

## Betano odds

Chance.cz has no ace or double-fault lines on WTA matches before they start.
Betano does: a ladder of "N or more" rungs for total aces, each player's
aces, and the same for double faults. `odds.py` reads them from Betano's own
JSON (`aces/betano.py`), stores every rung in `aces.odds`, and writes one
priced line per match and market to `aces.line`, which betken.cz's Tennis page
shows. A match's line freezes at kickoff.

```bash
python odds.py --dry-run    # print what it would store
python odds.py              # store and price - no advice
python odds.py --advise     # also mark bets that clear the edge
```

The posession server runs it every three hours (`deploy/run_aces.sh` in the
posession repo) - Betano refuses GitHub Actions runners - with `--advise`
since 1 Oct 2026 (WTA) and 2 Oct (ATP): aces only, at +10% EV or more, both players with at least
ten matches of history, a 30% model chance, the rung with the best Kelly growth.
If Betano ever answers with
a challenge page, the run stops and says so rather than trying to get past it.

Players are matched to tour ids by name (`aces/players.py`). Grand Slams use
their own player ids, so `model.aliases` folds each Slam id into the tour id
with the same name.
