// Chance.cz ace, double-fault and total-games lines, read in a browser tab.
//
// Chance.cz refuses requests that do not come from a browser, so this runs in
// an open chance.cz tab (with the user, in a session - never unattended). It
// asks the site's own JSON for tour-level ATP and WTA singles events, then
// each match starting within 30 hours, and keeps the two-sided lines:
//
//   [match id, start, competition, match name, home, away,
//    market (aces | aces:p | df | df:p | games), player box name, line, over, under]
//
// Gently: Chance.cz blocked the browser once (5 Oct 2026, "OVĚŘENÍ") after
// ~100 match requests a day. Since then: tour-level events only (Challengers
// never carry ace markets), matches within 30 hours, one request every two
// seconds, and a stop on the first answer that is not JSON. If the tab shows
// "OVĚŘENÍ" or "Okamžik", do not run this - wait.
//
// Run it in the tab, wait for window.__chance.state === 'done', then save
// JSON.stringify(window.__chance.lines) to cache/chance/<date>.json and run:
//   python load_chance.py cache/chance/<date>.json --advise
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const C = window.__chance = {state: 'listing', matches: [], lines: [], fetched: 0};
  const getJSON = async (u, opt) => {
    const r = await fetch(u, Object.assign({credentials: 'include'}, opt || {}));
    if (!(r.headers.get('content-type') || '').includes('json'))
      throw new Error('not JSON (HTTP ' + r.status + ') - stopping');
    return r.json();
  };
  const sports = await getJSON('/rest/offer/v6/sports?fromResults=false&withLive=false&mySelectionWithLiveMatches=false');
  const comps = [];
  const walk = (x, path) => {
    if (Array.isArray(x)) return x.forEach(v => walk(v, path));
    if (x && typeof x === 'object') {
      const nm = x.title || x.name, p = nm ? path.concat(nm) : path;
      if (x.type === 'COMPETITION' && x.id && /Tenis (muži|ženy) - dvouhra/.test(path.join('|'))) comps.push({id: x.id, name: nm});
      for (const v of Object.values(x)) if (v && typeof v === 'object') walk(v, p);
    }
  };
  walk(sports, []);
  // Tour-level events by city (Chance's Czech names), and the Slams.
  const TOUR = /^(ATP|WTA) (Peking|Tokyo|Šanghaj|Shanghai|Wuhan|Ningbo|Almaty|Stockholm|Antverpy|Antwerpen|Basilej|Vídeň|Paříž|Turín|Osaka|Hongkong|Guangzhou|Tokio|Soul|Rijád|Dauhá|Dubaj|Indian Wells|Miami|Madrid|Řím|Cincinnati|Montreal|Toronto|Washington|Halle|Queen's|Brisbane|Adelaide|Auckland|Acapulco|Rotterdam|Barcelona|Mnichov|Hamburk|Kitzbühel|Gstaad|Umag|Los Cabos|Atlanta|Winston-Salem|Eastbourne|Mallorca|Stuttgart|Berlín|Bad Homburg|Charleston|Linec|Ostrava|Praha)|Australian|Roland|Wimbledon|US Open/i;
  const want = comps.filter(c => TOUR.test(c.name) && !/4hra/.test(c.name));
  C.comps = want.map(c => c.name);
  for (const c of want) {
    const d = await getJSON('/rest/offer/v2/offer?limit=75', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({results: false, highlightAnyTime: false, limit: 75, order: 'DATESTART', type: 'COMPETITION', id: c.id, matchViewFilters: [], withLive: false})});
    const w = x => {
      if (Array.isArray(x)) return x.forEach(w);
      if (x && typeof x === 'object') {
        if (x.id && (x.nameFull || x.name) && x.dateClosed && !x.inLive && String(x.id).length >= 6)
          C.matches.push({id: x.id, name: x.nameFull || x.name, start: x.dateClosed, comp: c.name});
        else for (const v of Object.values(x)) if (v && typeof v === 'object') w(v);
      }
    };
    w(d);
    await sleep(2000);
  }
  const seen = new Set();
  const soon = C.matches.filter(m => new Date(m.start) - Date.now() < 30 * 3600e3 && !seen.has(m.id) && seen.add(m.id));
  C.todo = soon.length; C.state = 'fetching';
  const MK = {TOTAL_ACES: 'aces', TOTAL_ACES_PARTICIPANT: 'aces:p', TOTAL_DOUBLE_FAULTS: 'df', TOTAL_DOUBLE_FAULTS_PARTICIPANT: 'df:p'};
  for (const m of soon) {
    const d = await getJSON('/rest/offer/v3/matches/' + m.id + '?fromResults=false&ticketBuilderId=1');
    C.fetched++;
    const mt = d.match;
    const names = ps => (ps || []).map(p => p.name || p.nameFull || p.participantName).join('/');
    for (const t of mt.eventTables || []) {
      // Total games ("Počet gamů v zápasu") is kept for the match-length model.
      const mk = MK[(t.mySelectionId || '').replace(/^\d+-/, '').replace(/-\d+$/, '')]
                 || (t.name === 'Počet gamů v zápasu' ? 'games' : null);
      if (!mk) continue;
      for (const b of t.boxes || []) {
        // One box can hold several lines (games: 16.5, 17.5, ...): one entry each.
        const byLine = new Map();
        for (const cell of b.cells || []) {
          const g = /(Méně|Více) než ([\d.]+)/.exec(cell.name || '');
          if (!g || !cell.active) continue;
          const q = byLine.get(+g[2]) || {};
          q[g[1] === 'Více' ? 'over' : 'under'] = cell.odd;
          byLine.set(+g[2], q);
        }
        for (const [line, o] of byLine)
          C.lines.push([m.id, m.start, m.comp, mt.nameFull || m.name, names(mt.homeParticipants),
                        names(mt.visitingParticipants), mk, b.name || '', line, o.over || null, o.under || null]);
      }
    }
    await sleep(2000);
  }
  C.state = 'done';
})().catch(e => { window.__chance.state = 'stopped: ' + e.message; });
