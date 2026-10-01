// Chance.cz ace and double-fault lines, read in a browser tab on chance.cz.
//
// Chance.cz refuses requests that do not come from a browser, so this runs in
// an open chance.cz tab (with the user, in a session - never unattended). It
// asks the site's own JSON for every ATP and WTA singles competition, then each
// match, and keeps the two-sided ace and DF lines:
//
//   [match id, start, competition, match name, home, away,
//    market (aces | aces:p | df | df:p), player box name, line, over, under]
//
// Paste into the tab's console (or run through the browser tool), wait for
// window.__chance.state === 'done', then save JSON.stringify(window.__chance.lines)
// to cache/chance/<date>.json and run: python load_chance.py cache/chance/<date>.json
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const C = window.__chance = {state: 'listing', matches: [], lines: [], fetched: 0};
  const sports = await (await fetch('/rest/offer/v6/sports?fromResults=false&withLive=false&mySelectionWithLiveMatches=false', {credentials: 'include'})).json();
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
  const want = comps.filter(c => /^(ATP|WTA)|Australian|Roland|Wimbledon|US Open/i.test(c.name) && !/4hra/.test(c.name));
  for (const c of want) {
    const d = await (await fetch('/rest/offer/v2/offer?limit=75', {method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({results: false, highlightAnyTime: false, limit: 75, order: 'DATESTART', type: 'COMPETITION', id: c.id, matchViewFilters: [], withLive: false})})).json();
    const w = x => {
      if (Array.isArray(x)) return x.forEach(w);
      if (x && typeof x === 'object') {
        if (x.id && (x.nameFull || x.name) && x.dateClosed && !x.inLive && String(x.id).length >= 6)
          C.matches.push({id: x.id, name: x.nameFull || x.name, start: x.dateClosed, comp: c.name});
        else for (const v of Object.values(x)) if (v && typeof v === 'object') w(v);
      }
    };
    w(d);
    await sleep(1000);
  }
  C.state = 'fetching';
  const MK = {TOTAL_ACES: 'aces', TOTAL_ACES_PARTICIPANT: 'aces:p', TOTAL_DOUBLE_FAULTS: 'df', TOTAL_DOUBLE_FAULTS_PARTICIPANT: 'df:p'};
  for (const m of C.matches) {
    let d;
    try { d = await (await fetch('/rest/offer/v3/matches/' + m.id + '?fromResults=false&ticketBuilderId=1', {credentials: 'include'})).json(); }
    catch (e) { continue; }
    C.fetched++;
    const mt = d.match;
    const names = ps => (ps || []).map(p => p.name || p.nameFull || p.participantName).join('/');
    for (const t of mt.eventTables || []) {
      const mk = MK[(t.mySelectionId || '').replace(/^\d+-/, '').replace(/-\d+$/, '')];
      if (!mk) continue;
      for (const b of t.boxes || []) {
        const o = {}; let line = null;
        for (const cell of b.cells || []) {
          const g = /(Méně|Více) než ([\d.]+)/.exec(cell.name || '');
          if (!g || !cell.active) continue;
          line = +g[2]; o[g[1] === 'Více' ? 'over' : 'under'] = cell.odd;
        }
        if (line !== null) C.lines.push([m.id, m.start, m.comp, mt.nameFull || m.name, names(mt.homeParticipants),
                                         names(mt.visitingParticipants), mk, b.name || '', line, o.over || null, o.under || null]);
      }
    }
    await sleep(1000);
  }
  C.state = 'done';
})().catch(e => { window.__chance.state = 'crashed: ' + e; });
