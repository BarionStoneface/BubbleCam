'use strict';
/* BubbleCam front end. Plain JS, no build step: the Pi serves these files as they are.
   Pages: #tanks, #tank/<id>, #moments, #timelapses, #trends, #setup, #draw/<id>, #advanced */

// Helpers

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const el = html => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function api(url, opts = {}) {
  const r = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch (e) { /* not json */ }
    throw new Error(typeof msg === 'string' ? msg : 'Something went wrong');
  }
  return r.json();
}
const send = (url, method, body) => api(url, { method, body: body === undefined ? undefined : JSON.stringify(body) });

const clock = ts => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
const dayTime = ts => new Date(ts * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' });
const dayName = ts => new Date(ts * 1000).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
const mins = m => { m = Math.round(m); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ''}`; };
const dur = s => s < 60 ? `${Math.max(1, Math.round(s))} s` : mins(s / 60);
const ago = ts => mins((Date.now() / 1000 - ts) / 60);
const toLocalInput = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function flash(msg) {
  const t = el(`<div class="card" style="position:fixed;left:50%;bottom:90px;transform:translateX(-50%);z-index:50;padding:10px 16px">${esc(msg)}</div>`);
  document.body.append(t);
  setTimeout(() => t.remove(), 2600);
}
async function attempt(fn) {
  try { return await fn(); } catch (e) { flash(e.message); }
}

// App state, routing, header

let status = null;
let page = null;           // { onStatus?, leave? }
const view = $('#view');
const params = new URLSearchParams(location.search);
if (params.has('kiosk')) document.body.classList.add('kiosk');

const routes = {
  tanks: pageTanks, tank: pageTank, moments: pageMoments, timelapses: pageTimelapses,
  trends: pageTrends, setup: pageSetup, draw: pageDraw, advanced: pageAdvanced,
};
const TOP_LEVEL = ['tanks', 'moments', 'timelapses', 'trends'];

async function route() {
  const [name, arg] = (location.hash.slice(1) || (document.body.classList.contains('kiosk') ? 'tank/live' : 'tanks')).split('/');
  if (page && page.leave) page.leave();
  page = null;
  stopStreams();
  view.innerHTML = '';
  $$('#tabs a').forEach(a => a.classList.toggle('on', a.dataset.tab === name || (name === 'tank' && a.dataset.tab === 'tanks')));
  $('#back').hidden = TOP_LEVEL.includes(name);
  const fn = routes[name] || pageTanks;
  try { page = (await fn(arg)) || {}; } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  if (status && page && page.onStatus) page.onStatus(status);
}
addEventListener('hashchange', route);
$('#back').onclick = () => history.length > 1 ? history.back() : (location.hash = '#tanks');
$('#gear').onclick = () => { location.hash = '#setup'; };
const setTitle = t => { $('#title').textContent = t; document.title = t === 'BubbleCam' ? t : `${t} · BubbleCam`; };

function stopStreams() { $$('img[data-stream]').forEach(img => { img.src = ''; }); }

async function pollStatus() {
  try {
    status = await api('/api/status');
    const h = status.health;
    const btn = $('#health');
    btn.className = 'health ' + h.level;
    btn.querySelector('span').textContent = h.level === 'ok' ? 'All good' : h.items.length === 1 ? 'Needs a look' : `${h.items.length} things`;
    if (page && page.onStatus) page.onStatus(status);
  } catch (e) {
    $('#health').className = 'health problem';
    $('#health span').textContent = "Can't reach BubbleCam";
  }
}
setInterval(() => { if (!document.hidden) pollStatus(); }, 3000);

$('#health').onclick = () => {
  const h = status && status.health;
  const items = h && h.items.length ? h.items.map(i => `<div class="list-row"><div class="grow"><b>${esc(i.message)}</b>
      <div class="dim">Since ${dayTime(i.since)}</div></div></div>`).join('') : '<p>Everything is working: the camera is sending pictures, recording is running and there\'s room on the disk.</p>';
  const checkin = h && h.checkin === false ? '<p class="dim">The outside check in ping is failing.</p>' : '';
  openSheet(`<h3 style="margin-bottom:8px">System status</h3>${items}${checkin}`);
};

function openSheet(html) {
  $('#sheet-body').innerHTML = html;
  $('#sheet').showModal();
  return $('#sheet-body');
}
$('#sheet-close').onclick = () => $('#sheet').close();

// Words for what an animal is doing

function spotState(s) {
  const p = s.presence || {};
  const since = () => !p.since ? '' : Date.now() / 1000 - p.since < 60 ? ' just now' : ` for ${ago(p.since)}`;
  if (p.state === 'home') return { cls: 'home', text: `Home${since()}` };
  if (p.state === 'away') return { cls: 'away', text: `Out${since()}` };
  if (!s.has_areas && !s.has_home) return { cls: '', text: 'Not set up yet' };
  if (s.moving) return { cls: 'moving', text: 'Moving now' };
  if (s.has_home && p.examples && (!p.examples.home || !p.examples.away)) return { cls: '', text: 'Tap to teach home and out' };
  return { cls: '', text: 'Quiet' };
}

function spotLine(s) {
  if (s.outings) {
    let line = `${s.name} came out ${plural(s.outings, 'time')}, ${mins(s.out_minutes)} in all`;
    if (s.first) line += ` (first seen ${clock(s.first)})`;
    return line;
  }
  if (s.moving_minutes >= 1) return `${s.name} was moving for ${mins(s.moving_minutes)}${s.first ? ` (from ${clock(s.first)})` : ''}`;
  return `${s.name} stayed out of sight`;
}

function nightLines(n) {
  const lines = n.spots.map(spotLine);
  const extra = [];
  if (n.feedings) extra.push(plural(n.feedings, 'feeding'));
  if (n.bursts) extra.push(plural(n.bursts, 'burst') + ' of activity');
  if (extra.length) lines.push(extra.join(', ').replace(/^./, c => c.toUpperCase()));
  if (!lines.length) lines.push(`Watched for ${Math.round(n.watched_hours)} h`);
  return lines;
}

const momentLabel = ev => ev.kind === 'spot' ? (ev.spot_name || 'Spotlight') : ev.kind === 'feeding' ? 'Feeding' : 'Burst';

// Tanks (home)

async function pageTanks() {
  setTitle('BubbleCam');
  const tanks = await api('/api/tanks');
  const wrap = el('<div style="display:grid;gap:var(--gap)"></div>');
  const banner = el('<div class="banner" hidden></div>');
  wrap.append(banner);
  if (!tanks.length) {
    wrap.append(el('<div class="empty">Getting set up. Your first tank appears here a few seconds after the camera starts.</div>'));
    view.append(wrap);
    const timer = setInterval(() => route(), 5000);
    return { leave: () => clearInterval(timer) };
  }
  const grid = el('<div class="tank-grid"></div>');
  for (const t of tanks) {
    const card = el(`<a class="card tank-card" href="#tank/${t.id}">
      <div class="pic"><img alt=""><span class="badge${t.live ? ' on-air' : ''}">${t.live ? 'Live' : 'Last seen'}</span></div>
      <div class="body"><h3>${esc(t.name)}</h3><div class="lines dim"></div></div></a>`);
    const img = $('img', card);
    img.src = t.live ? `/api/snapshot.jpg?width=640&t=${Date.now()}` : `/api/tanks/${t.id}/thumb.jpg`;
    img.onerror = () => { img.style.visibility = 'hidden'; };
    const lines = $('.lines', card);
    const ln = t.last_night;
    let html = '';
    if (t.maintenance_until) html += `<div style="color:var(--warm)">Maintenance until ${clock(t.maintenance_until)}</div>`;
    if (t.live && t.spotlights.length) html += t.spotlights.map(s => `<div>${esc(s.name)}: ${esc(spotState(s).text)}</div>`).join('');
    else if (t.spotlights.length) html += `<div>Watching ${esc(t.spotlights.map(s => s.name).join(', '))}</div>`;
    if (ln && ln.watched_hours > 0) html += `<div>Last night: ${esc(nightLines(ln)[0])}</div>`;
    lines.innerHTML = html || '<div>No animals spotlighted yet</div>';
    card.dataset.id = t.id;
    grid.append(card);
  }
  wrap.append(grid);
  view.append(wrap);

  const refresh = setInterval(() => {
    $$('.tank-card', grid).forEach(c => {
      const t = tanks.find(x => String(x.id) === c.dataset.id);
      if (t && t.live) $('img', c).src = `/api/snapshot.jpg?width=640&t=${Date.now()}`;
    });
  }, 5000);
  return {
    leave: () => clearInterval(refresh),
    onStatus: st => {
      const unknown = st.view.status === 'unknown' && tanks.length;
      banner.hidden = !unknown;
      if (unknown && !banner.dataset.shown) {
        banner.dataset.shown = '1';
        banner.innerHTML = `<b>The camera doesn't recognise what it's looking at.</b>
          <p class="dim" style="margin:4px 0 10px">If you moved it, tell it which tank this is.</p><div class="row"></div>`;
        const row = $('.row', banner);
        for (const t of tanks) {
          const b = el(`<button class="btn small">It's ${esc(t.name)}</button>`);
          b.onclick = () => attempt(async () => { await send(`/api/tanks/${t.id}/this-is`, 'POST'); flash(`Got it: ${t.name}`); route(); });
          row.append(b);
        }
        const nb = el('<button class="btn small primary">It\'s a new tank</button>');
        nb.onclick = () => newTankHere();
        row.append(nb);
      }
    },
  };
}

async function newTankHere() {
  const name = prompt('What do you call this tank?', '');
  if (name === null) return;
  await attempt(async () => {
    const t = await send('/api/tanks', 'POST', { name });
    flash(`${t.name} set up`);
    location.hash = `#tank/${t.id}`;
  });
}

// One tank

async function pageTank(arg) {
  let tanks = await api('/api/tanks');
  if (arg === 'live') {
    const live = tanks.find(t => t.live);
    arg = live ? String(live.id) : (tanks[0] && String(tanks[0].id));
    if (!arg) return pageTanks();
  }
  let tank = tanks.find(t => String(t.id) === String(arg));
  if (!tank) { location.hash = '#tanks'; return; }
  setTitle(tank.name);
  let overlay = true;

  const liveBox = el(`<div class="live"><img data-stream alt="live picture"><div class="tools"><button class="chip" type="button">Boxes on</button></div></div>`);
  const liveImg = $('img', liveBox);
  const boxesBtn = $('.chip', liveBox);
  const spots = el('<div class="spots"></div>');
  const buttons = el(`<div class="big-buttons"><button class="btn" id="fed">🍤 Fed them</button><button class="btn" id="maint">🧽 Doing maintenance</button></div>`);
  const night = el('<div class="card" style="display:grid;gap:12px"><h3>Last night</h3><div class="dim">Loading…</div></div>');
  const day = el('<div class="card" style="display:grid;gap:12px"></div>');
  view.append(liveBox, spots, buttons, night, day);

  function setStream() {
    if (tank.live) {
      liveImg.src = `/stream.mjpg?overlay=${overlay ? 1 : 0}&width=960&t=${Date.now()}`;
      boxesBtn.hidden = false;
      $('.note', liveBox)?.remove();
    } else {
      liveImg.src = `/api/tanks/${tank.id}/thumb.jpg?t=${Date.now()}`;
      boxesBtn.hidden = true;
      if (!$('.note', liveBox)) liveBox.append(el('<div class="note">The camera is pointed at another tank right now. This is the last picture from this one.</div>'));
    }
  }
  boxesBtn.onclick = () => { overlay = !overlay; boxesBtn.textContent = overlay ? 'Boxes on' : 'Boxes off'; setStream(); };
  setStream();

  function drawSpots(list) {
    spots.innerHTML = '';
    if (!list.length) {
      spots.append(el(`<div class="card dim">No animals spotlighted in this tank. <a href="#setup">Pick who to watch</a></div>`));
      return;
    }
    for (const s of list) {
      const st = tank.live ? spotState(s) : { cls: '', text: 'Not in view right now' };
      const b = el(`<button class="card spot" type="button"><span class="dot ${st.cls}"></span>
        <span class="grow"><b>${esc(s.name)}</b><div class="dim">${esc(st.text)}</div></span></button>`);
      b.onclick = () => teachSheet(s);
      spots.append(b);
    }
  }
  drawSpots(tank.spotlights);

  function teachSheet(s) {
    if (!tank.live) return flash('The camera is pointed at another tank right now');
    if (!s.has_home) {
      openSheet(`<h3>${esc(s.name)}</h3><p>To tell when ${esc(s.name)} is home or out, draw a home box (a cave mouth or a perch) in Setup.</p>
        <a class="btn primary" href="#draw/${tank.id}" onclick="document.getElementById('sheet').close()">Draw boxes</a>`);
      return;
    }
    const ex = s.presence.examples;
    const body = openSheet(`<h3>Is ${esc(s.name)} home right now?</h3>
      <p class="dim">Each answer teaches BubbleCam what home and out look like. A few of each, with the light on and off, is plenty.</p>
      <div class="row"><button class="btn primary" data-l="home">Yes, home</button><button class="btn" data-l="away">No, out</button></div>
      <p class="dim" id="excount">So far: ${ex.home} home, ${ex.away} out.</p>`);
    $$('[data-l]', body).forEach(b => b.onclick = () => attempt(async () => {
      const r = await send(`/api/spotlights/${s.id}/teach?label=${b.dataset.l}`, 'POST');
      $('#excount', body).textContent = `So far: ${r.examples.home} home, ${r.examples.away} out. Thanks.`;
    }));
  }

  // Fed and maintenance
  $('#fed', buttons).onclick = () => attempt(async () => {
    const r = await send(`/api/tanks/${tank.id}/fed`, 'POST');
    flash(r.merged ? 'Feeding was already noticed, now confirmed' : 'Feeding logged');
  });
  const maintBtn = $('#maint', buttons);
  function drawMaint(until) {
    maintBtn.classList.toggle('warm', !!until);
    maintBtn.textContent = until ? `🧽 Maintenance until ${clock(until)}, tap when done` : '🧽 Doing maintenance';
    maintBtn.onclick = () => attempt(async () => {
      if (until) { await send(`/api/tanks/${tank.id}/maintenance`, 'DELETE'); drawMaint(null); flash('Back to normal'); }
      else {
        const body = openSheet(`<h3>Maintenance</h3><p class="dim">For water changes and rearranging. Nothing counts as an event, and it's left out of the trends.</p>
          <div class="row">${[30, 60, 120].map(m => `<button class="btn" data-m="${m}">${mins(m)}</button>`).join('')}</div>`);
        $$('[data-m]', body).forEach(b => b.onclick = () => attempt(async () => {
          const r = await send(`/api/tanks/${tank.id}/maintenance?minutes=${b.dataset.m}`, 'POST');
          $('#sheet').close(); drawMaint(r.until);
        }));
      }
    });
  }
  drawMaint(tank.maintenance_until);

  // Last night
  (async () => {
    const [nights, tls] = await Promise.all([api(`/api/tanks/${tank.id}/nights?days=2`), api(`/api/timelapses?tank_id=${tank.id}`)]);
    const n = nights.find(x => !x.in_progress) || nights[0];
    if (!n) { night.innerHTML = '<h3>Last night</h3><div class="dim">Nothing recorded yet. Check back in the morning.</div>'; return; }
    night.innerHTML = `<div class="row"><h3 class="grow">${n.in_progress ? 'Tonight so far' : 'Last night'}</h3><span class="dim">${dayName(n.start)}</span></div>
      <div>${nightLines(n).map(l => `<div>${esc(l)}</div>`).join('')}</div><div class="row tl"></div><div class="moments-strip"></div>`;
    const tl = tls.find(x => Math.abs(x.start_ts - n.start) < 60 && x.status === 'done' && x.has_file);
    if (tl) {
      const b = el('<button class="btn primary">▶ Watch the timelapse</button>');
      b.onclick = () => playUrl(`/api/timelapses/${tl.id}.mp4`, `Timelapse, night of ${dayName(n.start)}`);
      $('.tl', night).append(b);
    } else if (!n.in_progress) {
      $('.tl', night).append(el('<span class="dim">The timelapse for this night isn\'t ready yet.</span>'));
    }
    const moms = (await api(`/api/moments?tank_id=${tank.id}&before=${n.end}&limit=60`)).filter(m => m.start_ts >= n.start && m.has_thumb);
    moms.sort((a, b) => (b.kind === 'spot') - (a.kind === 'spot') || b.peak - a.peak);
    const strip = $('.moments-strip', night);
    for (const m of moms.slice(0, 4)) {
      const b = el(`<button type="button"><img src="/api/moments/${m.id}/thumb.jpg" alt=""><span>${esc(momentLabel(m))} · ${clock(m.start_ts)}</span></button>`);
      b.onclick = () => openMoment(m);
      strip.append(b);
    }
  })().catch(e => { night.innerHTML = `<h3>Last night</h3><div class="dim">${esc(e.message)}</div>`; });

  const bar = dayBar(day, tank);

  return {
    leave: () => bar.leave(),
    onStatus: st => {
      const wasLive = tank.live;
      tank.live = st.tank && st.tank.id === tank.id;
      if (tank.live !== wasLive) setStream();
      if (tank.live) drawSpots(st.spotlights);
      drawMaint(tank.live ? st.maintenance_until : tank.maintenance_until);
    },
  };
}

// The day bar: one lane per spotlighted animal plus one for the whole tank, noon to noon.

function dayBar(card, tank) {
  let offset = 0;
  let data = null, start = 0, end = 0, cursor = null, selected = null;
  card.innerHTML = `<div class="row"><button class="btn small" data-d="-1" aria-label="Earlier night">‹</button>
      <h3 class="grow" style="text-align:center" id="dlabel"></h3><button class="btn small" data-d="1" aria-label="Later night">›</button></div>
    <div class="daybar"><canvas></canvas></div>
    <div class="legend"><span><i style="background:var(--accent)"></i>Moving</span><span><i style="background:var(--warm)"></i>Out of home</span>
      <span><i style="background:var(--feed)"></i>Feeding</span><span><i style="background:var(--burst)"></i>Burst</span>
      <span><i style="background:var(--line)"></i>Not watching this tank</span></div>
    <div class="preview" hidden><img alt="picture at this time"><div style="display:grid;gap:8px;align-content:start">
      <b id="ptime"></b><div class="dim" id="pmsg"></div><div class="row" id="pbtns"></div></div></div>
    <p class="dim" id="dhint" style="margin:0">Tap or drag along the bar to look back at any moment.</p>`;
  const canvas = $('canvas', card);
  const preview = $('.preview', card);
  const lanes = () => [...(data ? data.spotlights : tank.spotlights).map(s => ({ kind: 'spot', id: s.id, name: s.name })), { kind: 'tank', name: 'Whole tank' }];
  const LANE = 30, AXIS = 20;

  function windowFor(off) {
    const now = new Date();
    const s = new Date(now);
    s.setHours(12, 0, 0, 0);
    if (now < s) s.setDate(s.getDate() - 1);
    s.setDate(s.getDate() + off);
    return [s.getTime() / 1000, s.getTime() / 1000 + 86400];
  }

  async function load() {
    [start, end] = windowFor(offset);
    $('#dlabel', card).textContent = offset === 0 ? 'Tonight' : offset === -1 ? 'Last night' : `Night of ${dayName(start)}`;
    $('[data-d="1"]', card).disabled = offset >= 0;
    data = await api(`/api/tanks/${tank.id}/day?start=${start}&end=${end}`);
    draw();
  }
  $$('[data-d]', card).forEach(b => b.onclick = () => { offset += +b.dataset.d; cursor = null; selected = null; preview.hidden = true; load(); });

  function draw() {
    const L = lanes();
    const dpr = devicePixelRatio || 1, W = canvas.clientWidth, H = L.length * LANE + AXIS;
    canvas.style.height = H + 'px';
    canvas.width = W * dpr; canvas.height = H * dpr;
    const g = canvas.getContext('2d'); g.scale(dpr, dpr);
    const x = ts => (ts - start) / (end - start) * W;
    const now = Date.now() / 1000;
    g.clearRect(0, 0, W, H);
    L.forEach((lane, i) => {
      const y = i * LANE;
      g.fillStyle = css('--line'); g.fillRect(0, y + 2, W, LANE - 4);
      g.fillStyle = css('--surface-2');
      for (const [a, b] of data.watched) g.fillRect(x(a), y + 2, Math.max(1, x(b) - x(a)), LANE - 4);
    });
    // Maintenance shading across all lanes
    g.fillStyle = css('--warm'); g.globalAlpha = .18;
    for (const m of data.maintenance) g.fillRect(x(m.start_ts), 0, Math.max(2, x(Math.min(m.end_ts, end)) - x(m.start_ts)), L.length * LANE);
    g.globalAlpha = 1;
    L.forEach((lane, i) => {
      const y = i * LANE;
      if (lane.kind === 'spot') {
        const pres = data.presence[lane.id] || [];
        g.fillStyle = css('--warm'); g.globalAlpha = .55;
        pres.forEach((p, k) => {
          if (p.state !== 'away') return;
          const until = k + 1 < pres.length ? pres[k + 1].ts : Math.min(now, end);
          const a = Math.max(start, p.ts);
          if (until > a) g.fillRect(x(a), y + 5, Math.max(2, x(until) - x(a)), LANE - 10);
        });
        g.globalAlpha = 1;
      }
      for (const ev of data.events) {
        const mine = lane.kind === 'spot' ? ev.kind === 'spot' && ev.spot_id === lane.id : ev.kind !== 'spot';
        if (!mine) continue;
        const color = ev.kind === 'spot' ? css('--accent') : ev.kind === 'feeding' ? css('--feed') : css('--burst');
        const a = x(Math.max(ev.start_ts, start)), b = x(Math.min(ev.end_ts || now, end));
        const w = Math.max(3, b - a);
        if (ev.wrong) { g.strokeStyle = css('--dim'); g.lineWidth = 1; g.strokeRect(a + .5, y + 8.5, w - 1, LANE - 17); }
        else { g.fillStyle = color; g.fillRect(a, y + 8, w, LANE - 16); }
      }
      if (lane.kind === 'tank') {
        g.strokeStyle = css('--feed'); g.lineWidth = 2;
        for (const f of data.feedings) { g.beginPath(); g.moveTo(x(f.ts), y + 3); g.lineTo(x(f.ts), y + LANE - 3); g.stroke(); }
      }
      g.fillStyle = css('--dim'); g.font = '11px ' + css('--font');
      g.fillText(lane.name, 6, y + 13);
    });
    // Hour labels
    g.fillStyle = css('--dim'); g.font = '11px ' + css('--font');
    for (let hr = 0; hr <= 24; hr += 3) {
      const ts = start + hr * 3600;
      const label = new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric' });
      const tx = Math.min(Math.max(0, x(ts) - 12), W - 30);
      g.fillText(label, tx, H - 5);
    }
    if (now > start && now < end) {
      g.strokeStyle = css('--text'); g.globalAlpha = .5; g.lineWidth = 1; g.beginPath();
      g.moveTo(x(now), 0); g.lineTo(x(now), L.length * LANE); g.stroke(); g.globalAlpha = 1;
    }
    if (cursor) {
      g.strokeStyle = css('--accent'); g.lineWidth = 2; g.beginPath();
      g.moveTo(x(cursor), 0); g.lineTo(x(cursor), L.length * LANE); g.stroke();
    }
  }

  const tsAt = e => { const r = canvas.getBoundingClientRect(); return start + Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (end - start); };
  let dragging = false;
  canvas.onpointerdown = e => { dragging = true; canvas.setPointerCapture(e.pointerId); cursor = tsAt(e); draw(); showTime(); };
  canvas.onpointermove = e => { if (!dragging) return; cursor = tsAt(e); draw(); showTime(); };
  canvas.onpointerup = () => { dragging = false; pick(); };

  function showTime() { preview.hidden = false; $('#ptime', card).textContent = dayTime(cursor); $('#dhint', card).hidden = true; }

  function pick() {
    if (!cursor) return;
    const tol = (end - start) / canvas.clientWidth * 6;
    selected = data.events.find(ev => cursor >= ev.start_ts - tol && cursor <= (ev.end_ts || Date.now() / 1000) + tol) || null;
    const img = $('img', preview);
    const msg = $('#pmsg', card);
    const btns = $('#pbtns', card);
    btns.innerHTML = '';
    msg.textContent = '';
    if (cursor > Date.now() / 1000) { img.hidden = true; msg.textContent = "That's still to come."; return; }
    img.hidden = false;
    img.onload = null;
    img.onerror = () => { img.hidden = true; msg.textContent = 'No footage from this time. It may have been cleared out to make room.'; };
    img.src = `/api/frame.jpg?ts=${cursor.toFixed(1)}&width=960`;
    if (selected) {
      const b = el(`<button class="btn primary">▶ Watch: ${esc(momentLabel(selected))}, ${clock(selected.start_ts)}</button>`);
      b.onclick = () => openMoment({ ...selected, tank_name: tank.name });
      btns.append(b);
    }
    const p = el('<button class="btn">▶ Play from here</button>');
    p.onclick = () => playUrl(`/api/footage.mp4?start=${(cursor - 5).toFixed(1)}&end=${(cursor + 115).toFixed(1)}`, `${tank.name}, ${dayTime(cursor)}`);
    btns.append(p);
  }

  const onResize = () => data && draw();
  addEventListener('resize', onResize);
  const timer = setInterval(() => { if (offset === 0 && !dragging) load(); }, 60000);
  load().catch(e => { card.append(el(`<div class="dim">${esc(e.message)}</div>`)); });
  return { leave: () => { removeEventListener('resize', onResize); clearInterval(timer); } };
}

// Video player for moments and timelapses

let currentMoment = null;
function playUrl(src, title, info = '') {
  currentMoment = null;
  $('#pvideo').src = src;
  $('#ptitle').textContent = title;
  $('#pinfo').textContent = info;
  $('#pdownload').href = src + (src.includes('?') ? '&' : '?') + 'download=1';
  $('#ptags').innerHTML = '';
  ['#pstar', '#pwrong', '#pdelete'].forEach(s => { $(s).hidden = true; });
  $('#player').showModal();
  $('#pvideo').play().catch(() => {});
}

function openMoment(ev, onChange) {
  const src = `/api/moments/${ev.id}/clip.mp4`;
  playUrl(src, momentLabel(ev), `${ev.tank_name ? ev.tank_name + ' · ' : ''}${dayTime(ev.start_ts)}${ev.end_ts ? ' · ' + dur(ev.end_ts - ev.start_ts) : ''}`);
  currentMoment = { ev, onChange };
  $('#pdownload').href = src;
  const star = $('#pstar'), wrong = $('#pwrong'), del = $('#pdelete');
  [star, wrong, del].forEach(b => { b.hidden = false; });
  star.classList.toggle('on', !!ev.starred);
  star.onclick = () => attempt(async () => {
    ev.starred = !ev.starred;
    await send(`/api/moments/${ev.id}/star?on=${!!ev.starred}`, 'POST');
    star.classList.toggle('on', !!ev.starred);
    onChange && onChange(ev);
  });
  const drawWrong = () => { wrong.textContent = ev.wrong ? 'Marked as wrong (undo)' : 'This was wrong'; };
  drawWrong();
  wrong.onclick = () => attempt(async () => {
    ev.wrong = ev.wrong ? 0 : 1;
    await send(`/api/moments/${ev.id}/wrong?on=${!!ev.wrong}`, 'POST');
    drawWrong();
    flash(ev.wrong ? "Thanks. It won't count in summaries, and it helps tune the tank." : 'Restored');
    onChange && onChange(ev);
  });
  del.onclick = () => attempt(async () => {
    if (!confirm('Delete this moment for good?')) return;
    await send(`/api/moments/${ev.id}`, 'DELETE');
    $('#player').close();
    onChange && onChange(ev, true);
  });
  drawTags(ev, onChange);
}

function drawTags(ev, onChange) {
  const box = $('#ptags');
  const tags = ev.tags || [];
  box.innerHTML = '<span class="dim">Tags:</span>';
  for (const t of tags) {
    const chip = el(`<span class="tag">${esc(t)}<button aria-label="Remove tag">×</button></span>`);
    $('button', chip).onclick = () => save(tags.filter(x => x !== t));
    box.append(chip);
  }
  const input = el('<input type="text" placeholder="add a tag" maxlength="40" list="taglist">');
  input.onkeydown = e => { if (e.key === 'Enter' && input.value.trim()) save([...tags, input.value.trim()]); };
  input.onchange = () => { if (input.value.trim()) save([...tags, input.value.trim()]); };
  box.append(input);
  api('/api/tags').then(all => {
    const dl = el(`<datalist id="taglist">${all.map(t => `<option value="${esc(t.tag)}">`).join('')}</datalist>`);
    $('#taglist')?.remove();
    box.append(dl);
  });
  async function save(next) {
    await attempt(async () => {
      const r = await send(`/api/moments/${ev.id}/tags`, 'PUT', next);
      ev.tags = r.tags;
      drawTags(ev, onChange);
      onChange && onChange(ev);
    });
  }
}
$('#pclose').onclick = () => $('#player').close();
$('#player').addEventListener('close', () => { $('#pvideo').pause(); $('#pvideo').removeAttribute('src'); $('#pvideo').load(); });

// Moments

async function pageMoments() {
  setTitle('Moments');
  const [tanks, tags] = await Promise.all([api('/api/tanks'), api('/api/tags')]);
  let filter = { key: 'all' };
  let tankId = '';
  let oldest = null;
  const filters = [{ key: 'all', label: 'All' }];
  for (const t of tanks) for (const s of t.spotlights) {
    const dup = tanks.some(o => o.id !== t.id && o.spotlights.some(x => x.name === s.name));
    filters.push({ key: 'spot' + s.id, label: dup ? `${s.name} (${t.name})` : s.name, q: { spot_id: s.id } });
  }
  filters.push({ key: 'feeding', label: 'Feeding', q: { kind: 'feeding' } }, { key: 'burst', label: 'Bursts', q: { kind: 'burst' } },
    { key: 'night', label: 'At night', q: { night_only: true } }, { key: 'starred', label: '★ Kept', q: { starred: true } });
  for (const t of tags) filters.push({ key: 'tag:' + t.tag, label: '#' + t.tag, q: { tag: t.tag } });
  filters.push({ key: 'wrong', label: 'Marked wrong', q: { wrong: true } });

  const bar = el('<div style="display:grid;gap:10px"></div>');
  if (tanks.length > 1) {
    const sel = el(`<select><option value="">All tanks</option>${tanks.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('')}</select>`);
    sel.onchange = () => { tankId = sel.value; load(true); };
    bar.append(sel);
  }
  const chips = el('<div class="chips"></div>');
  for (const f of filters) {
    const c = el(`<button class="chip${f.key === 'all' ? ' on' : ''}" type="button">${esc(f.label)}</button>`);
    c.onclick = () => { filter = f; $$('.chip', chips).forEach(x => x.classList.toggle('on', x === c)); load(true); };
    chips.append(c);
  }
  bar.append(chips);
  const grid = el('<div class="grid"></div>');
  const more = el('<button class="btn" style="justify-self:center">Show older</button>');
  view.append(bar, grid, more);

  async function load(reset) {
    if (reset) { grid.innerHTML = ''; oldest = null; }
    const q = new URLSearchParams({ limit: 40, ...(filter.q || {}) });
    if (tankId) q.set('tank_id', tankId);
    if (oldest) q.set('before', oldest);
    const rows = await api('/api/moments?' + q);
    for (const ev of rows) {
      oldest = ev.start_ts;
      grid.append(momentCard(ev));
    }
    more.hidden = rows.length < 40;
    if (!grid.children.length) grid.append(el('<div class="empty" style="grid-column:1/-1">Nothing here yet.</div>'));
  }
  function momentCard(ev) {
    const card = el(`<div class="card moment${ev.wrong ? ' wrong' : ''}" role="button" tabindex="0">
      ${ev.has_thumb ? `<img loading="lazy" src="/api/moments/${ev.id}/thumb.jpg" alt="">` : '<div class="noimg"></div>'}
      <div class="meta"><div class="row"><span class="tag ${ev.kind}">${esc(momentLabel(ev))}</span><span class="grow"></span>
        <span class="star${ev.starred ? ' on' : ''}">★</span></div>
        <div class="dim">${tanks.length > 1 ? esc(ev.tank_name || '') + ' · ' : ''}${dayTime(ev.start_ts)}${ev.end_ts ? ' · ' + dur(ev.end_ts - ev.start_ts) : ' · happening now'}</div>
        ${ev.tags.length ? `<div class="row">${ev.tags.map(t => `<span class="tag">#${esc(t)}</span>`).join('')}</div>` : ''}</div></div>`);
    card.onclick = () => openMoment(ev, (changed, gone) => {
      if (gone) card.remove(); else card.replaceWith(momentCard(changed));
    });
    card.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); card.click(); } };
    return card;
  }
  more.onclick = () => load(false);
  await load(true);
}

// Timelapses

async function pageTimelapses() {
  setTitle('Timelapses');
  const [tanks, defaults] = await Promise.all([api('/api/tanks'), api('/api/defaults')]);
  const maker = el(`<div class="card" style="display:grid;gap:14px">
    <div class="row"><h3 class="grow">Make a timelapse</h3><button class="btn small" id="toggle">Show</button></div>
    <div id="form" hidden style="display:grid;gap:14px">
      ${tanks.length > 1 ? `<label class="field">Tank<select id="ttank">${tanks.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('')}</select></label>` : ''}
      <div class="chips"><button class="chip" data-p="night">Last night</button><button class="chip" data-p="24">Last 24 h</button><button class="chip" data-p="1">Last hour</button></div>
      <div class="row"><label class="field grow">From<input type="datetime-local" id="tstart"></label><label class="field grow">To<input type="datetime-local" id="tend"></label></div>
      <label class="field">When nothing's happening: <b id="maxv"></b><input type="range" id="maxs" min="1" max="3.5" step="0.01"></label>
      <label class="field">When something moves: <b id="minv"></b><input type="range" id="mins" min="1" max="30" step="1"></label>
      <label class="field">How gently it slows down and speeds up: <b id="easev"></b><input type="range" id="ease" min="0" max="120" step="5"></label>
      <label class="field">Slow down for<select id="driver"></select></label>
      <div class="row"><button class="btn primary" id="go">Make it</button><span class="dim" id="msg"></span></div>
    </div></div>`);
  const list = el('<div style="display:grid;gap:var(--gap)"></div>');
  view.append(maker, list);

  const form = $('#form', maker);
  $('#toggle', maker).onclick = () => { form.hidden = !form.hidden; $('#toggle', maker).textContent = form.hidden ? 'Show' : 'Hide'; };
  const tankSel = $('#ttank', maker);
  const curTank = () => tanks.find(t => String(t.id) === String(tankSel ? tankSel.value : tanks[0]?.id));
  const maxFrom = v => { const x = 10 ** v; return x < 100 ? Math.round(x) : Math.round(x / 10) * 10; };
  function labels() {
    $('#maxv', maker).textContent = maxFrom($('#maxs', maker).value) + 'x';
    $('#minv', maker).textContent = $('#mins', maker).value === '1' ? 'real time' : $('#mins', maker).value + 'x';
    $('#easev', maker).textContent = $('#ease', maker).value + ' s';
  }
  function drivers() {
    const t = curTank();
    const opts = [];
    if (t && t.spotlights.length) {
      opts.push(['spots', t.spotlights.length > 1 ? 'Any spotlighted animal' : t.spotlights[0].name]);
      if (t.spotlights.length > 1) t.spotlights.forEach(s => opts.push([`spot:${s.slot}`, s.name]));
      opts.push(['both', 'Animals or the whole tank']);
    }
    opts.push(['tank', 'The whole tank']);
    $('#driver', maker).innerHTML = opts.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('');
  }
  const range = (a, b) => { $('#tstart', maker).value = toLocalInput(a); $('#tend', maker).value = toLocalInput(b); };
  function lastNight() {
    const [sh, sm] = defaults.night_start.split(':').map(Number), [eh, em] = defaults.night_end.split(':').map(Number);
    const end = new Date(); end.setHours(eh, em, 0, 0);
    if (end > new Date()) end.setDate(end.getDate() - 1);
    const start = new Date(end); start.setHours(sh, sm, 0, 0);
    if (start >= end) start.setDate(start.getDate() - 1);
    range(start, end);
  }
  $$('[data-p]', maker).forEach(c => c.onclick = () => {
    const p = c.dataset.p;
    if (p === 'night') lastNight(); else range(new Date(Date.now() - p * 3600e3), new Date());
  });
  ['#maxs', '#mins', '#ease'].forEach(s => { $(s, maker).oninput = labels; });
  if (tankSel) tankSel.onchange = drivers;
  $('#maxs', maker).value = Math.log10(defaults.max_speed);
  $('#mins', maker).value = defaults.min_speed;
  $('#ease', maker).value = defaults.ease_seconds;
  lastNight(); labels(); drivers();
  $('#go', maker).onclick = () => attempt(async () => {
    const t = curTank();
    await send('/api/timelapses', 'POST', {
      tank_id: t ? t.id : null,
      start_ts: new Date($('#tstart', maker).value).getTime() / 1000, end_ts: new Date($('#tend', maker).value).getTime() / 1000,
      max_speed: maxFrom($('#maxs', maker).value), min_speed: +$('#mins', maker).value, ease_seconds: +$('#ease', maker).value,
      driver: $('#driver', maker).value,
    });
    $('#msg', maker).textContent = 'On its way. It appears below when ready.';
    load();
  });

  let timer = null;
  async function load() {
    const rows = await api('/api/timelapses');
    list.innerHTML = '';
    if (!rows.length) list.append(el('<div class="empty">Each tank\'s night becomes a timelapse every morning. They\'ll show up here.</div>'));
    for (const r of rows) {
      const p = r.params || {};
      const title = r.auto ? `Night of ${dayName(r.start_ts)}` : `${dayTime(r.start_ts)} to ${dayTime(r.end_ts)}`;
      const state = r.status === 'rendering' ? `<div style="height:6px;background:var(--line);border-radius:3px;overflow:hidden"><i style="display:block;height:100%;width:${Math.round(r.progress * 100)}%;background:var(--accent)"></i></div>`
        : r.status === 'queued' ? '<span class="dim">Waiting its turn</span>'
        : r.status === 'failed' ? `<span style="color:var(--bad)">Couldn't make this one: ${esc(r.error || '')}</span>` : '';
      const card = el(`<div class="card" style="display:grid;gap:10px"><div style="display:grid;gap:4px"><b>${esc(title)}</b>
        <div class="dim">${tanks.length > 1 && r.tank_name ? esc(r.tank_name) + ' · ' : ''}${p.max_speed}x when quiet, ${p.min_speed === 1 ? 'real time' : p.min_speed + 'x'} when moving</div>${state}</div>
        <div class="row">${r.has_file ? '<button class="btn primary" data-a="play">▶ Play</button>' : ''}
        <button class="star${r.starred ? ' on' : ''}" data-a="star" title="Keep forever">★</button><span class="grow"></span>
        ${r.has_file ? `<a class="btn small" href="/api/timelapses/${r.id}.mp4?download=1" download>Download</a>` : ''}
        <button class="btn small" data-a="del">Delete</button></div></div>`);
      card.onclick = e => attempt(async () => {
        const a = e.target.dataset.a;
        if (a === 'play') playUrl(`/api/timelapses/${r.id}.mp4`, title);
        if (a === 'star') { await send(`/api/timelapses/${r.id}/star?on=${!r.starred}`, 'POST'); load(); }
        if (a === 'del' && confirm('Delete this timelapse?')) { await send(`/api/timelapses/${r.id}`, 'DELETE'); load(); }
      });
      list.append(card);
    }
    clearTimeout(timer);
    if (rows.some(r => r.status === 'queued' || r.status === 'rendering')) timer = setTimeout(load, 3000);
  }
  await load();
  return { leave: () => clearTimeout(timer) };
}

// Trends

async function pageTrends() {
  setTitle('Trends');
  const tanks = await api('/api/tanks');
  if (!tanks.length) { view.append(el('<div class="empty">No tanks yet.</div>')); return; }
  let tank = tanks.find(t => t.live) || tanks[0];
  let days = 30;
  const controls = el('<div style="display:grid;gap:10px"></div>');
  const out = el('<div style="display:grid;gap:var(--gap)"></div>');
  view.append(controls, out);
  if (tanks.length > 1) {
    const chips = el('<div class="chips"></div>');
    for (const t of tanks) {
      const c = el(`<button class="chip${t === tank ? ' on' : ''}">${esc(t.name)}</button>`);
      c.onclick = () => { tank = t; $$('.chip', chips).forEach(x => x.classList.toggle('on', x === c)); load(); };
      chips.append(c);
    }
    controls.append(chips);
  }
  const span = el('<div class="chips"></div>');
  for (const d of [14, 30, 90]) {
    const c = el(`<button class="chip${d === days ? ' on' : ''}">${d} nights</button>`);
    c.onclick = () => { days = d; $$('.chip', span).forEach(x => x.classList.toggle('on', x === c)); load(); };
    span.append(c);
  }
  controls.append(span);

  async function load() {
    out.innerHTML = '<div class="dim">Loading…</div>';
    const [tr, feeds] = await Promise.all([api(`/api/tanks/${tank.id}/trends?days=${days}`), api(`/api/tanks/${tank.id}/feedings?days=${days}`)]);
    out.innerHTML = '';
    if (!tr.nights.length) { out.append(el('<div class="empty">Trends appear once there are a few nights of footage.</div>')); }
    else {
      for (const s of tr.spotlights) {
        const key = s.has_home ? 'out_minutes' : 'moving_minutes';
        const card = el(`<div class="card" style="display:grid;gap:8px"><h3>${esc(s.name)}</h3>
          <div class="dim">${s.has_home ? 'Minutes out of home each night' : 'Minutes moving each night'}</div><canvas class="chart"></canvas></div>`);
        out.append(card);
        barChart($('canvas', card), tr.nights.map(n => ({ label: n.night, value: (n.spots[s.id] || {})[key] || 0, marked: n.maintenance })), css('--accent'), v => mins(v));
      }
      const card = el(`<div class="card" style="display:grid;gap:8px"><h3>Whole tank</h3><div class="dim">How busy the tank was each night</div><canvas class="chart"></canvas></div>`);
      out.append(card);
      barChart($('canvas', card), tr.nights.map(n => ({ label: n.night, value: (n.tank_activity || 0) * 1000, marked: n.maintenance })), css('--burst'), v => v.toFixed(0));
      out.append(el('<div class="legend"><span><i style="background:var(--line)"></i>Maintenance that night</span></div>'));
    }
    const fcard = el(`<div class="card scroll-x"><h3 style="margin-bottom:8px">Feedings</h3><table><thead><tr><th>When</th><th>Logged by</th><th>First to show up</th><th>Tank got busier</th></tr></thead><tbody></tbody></table></div>`);
    const tb = $('tbody', fcard);
    for (const f of feeds) {
      const tr2 = el(`<tr><td>${dayTime(f.ts)}</td><td>${f.source === 'auto' ? 'Noticed by BubbleCam' : 'Fed them button'}</td>
        <td>${f.emerged ? `${esc(f.emerged.name)} after ${dur(f.emerged.after_s)}` : '<span class="dim">no one yet</span>'}</td>
        <td>${f.response ? f.response + 'x' : ''}</td></tr>`);
      tb.append(tr2);
    }
    if (!feeds.length) tb.append(el('<tr><td colspan="4" class="dim">No feedings logged yet. Tap "Fed them" on the tank page, or BubbleCam notices the rush by itself.</td></tr>'));
    out.append(fcard);
  }
  await load();
}

function barChart(canvas, points, color, fmt) {
  const dpr = devicePixelRatio || 1, W = canvas.clientWidth || 600, H = 170, pad = 22;
  canvas.style.height = H + 'px'; canvas.width = W * dpr; canvas.height = H * dpr;
  const g = canvas.getContext('2d'); g.scale(dpr, dpr);
  const max = Math.max(1e-9, ...points.map(p => p.value));
  const bw = W / Math.max(points.length, 1);
  g.strokeStyle = css('--line'); g.lineWidth = 1;
  for (const f of [.5, 1]) { const y = H - pad - (H - pad - 14) * f; g.beginPath(); g.moveTo(0, y + .5); g.lineTo(W, y + .5); g.stroke(); }
  points.forEach((p, i) => {
    const h = (H - pad - 14) * p.value / max;
    g.fillStyle = p.marked ? css('--line') : color;
    g.fillRect(i * bw + bw * .15, H - pad - h, bw * .7, h);
  });
  g.fillStyle = css('--dim'); g.font = '11px ' + css('--font');
  g.fillText(fmt(max), 2, 11);
  const every = Math.ceil(points.length / 6);
  points.forEach((p, i) => { if (i % every === 0) g.fillText(p.label, i * bw, H - 6); });
}

// Setup

async function pageSetup() {
  setTitle('Setup');
  const [tanks, st, notify] = await Promise.all([api('/api/tanks'), api('/api/status'), api('/api/notify')]);
  const live = tanks.find(t => t.live);

  const tankCard = el(`<div class="card"><h3 style="margin-bottom:6px">Tanks</h3><div id="tlist"></div>
    <div class="row" style="margin-top:12px"><button class="btn" id="newtank">Set up a new tank from what the camera sees</button></div></div>`);
  for (const t of tanks) {
    const row = el(`<div class="list-row"><input type="text" class="grow" value="${esc(t.name)}" aria-label="Tank name">
      <span class="dim">${t.live ? 'Camera is looking at this one' : ''}</span>
      ${!t.live ? '<button class="btn small" data-a="here">The camera is on this one</button>' : ''}
      <button class="btn small" data-a="del">Delete</button></div>`);
    $('input', row).onchange = e => attempt(() => send(`/api/tanks/${t.id}`, 'PATCH', { name: e.target.value }));
    row.onclick = e => attempt(async () => {
      if (e.target.dataset.a === 'here') { await send(`/api/tanks/${t.id}/this-is`, 'POST'); flash(`Now watching ${t.name}`); route(); }
      if (e.target.dataset.a === 'del' && confirm(`Delete ${t.name}? Its boxes and settings go. Recordings stay until they'd normally be cleared out.`)) {
        await send(`/api/tanks/${t.id}`, 'DELETE'); route();
      }
    });
    $('#tlist', tankCard).append(row);
  }
  $('#newtank', tankCard).onclick = () => newTankHere();
  view.append(tankCard);

  if (live) {
    const t = live;
    const spotCard = el(`<div class="card"><h3>Who to watch in ${esc(t.name)}</h3>
      <p class="dim" style="margin:6px 0 10px">1. Add an animal. 2. Draw where it hangs out, and its home if it has one, like a cave or a perch.
      3. On the tank page, tap the animal and tell BubbleCam when it's home and when it's out.</p>
      <div id="slist"></div>
      <div class="row" style="margin-top:12px"><input type="text" id="newname" placeholder="Name, e.g. Pleco" class="grow" maxlength="30">
        <button class="btn primary" id="add">Add</button></div>
      <div class="row" style="margin-top:14px"><a class="btn" href="#draw/${t.id}">✏️ Draw boxes</a>
        <span class="dim">Where each animal hangs out, its home, and areas to ignore like bubbles.</span></div></div>`);
    const layout = await api(`/api/tanks/${t.id}/layout`);
    for (const s of layout.spotlights) {
      const live_s = st.spotlights.find(x => x.id === s.id) || {};
      const ex = (live_s.presence || {}).examples || { home: 0, away: 0 };
      const row = el(`<div class="list-row"><input type="text" class="grow" value="${esc(s.name)}" aria-label="Name">
        <span class="dim">${s.areas.length ? plural(s.areas.length, 'area') : 'no areas yet'} · ${s.home ? `home box, taught ${ex.home} home / ${ex.away} out` : 'no home box'}</span>
        ${s.home ? '<button class="btn small" data-a="forget">Forget home lessons</button>' : ''}
        <button class="btn small" data-a="rm">Remove</button></div>`);
      $('input', row).onchange = e => attempt(() => send(`/api/spotlights/${s.id}`, 'PATCH', { name: e.target.value }));
      row.onclick = e => attempt(async () => {
        if (e.target.dataset.a === 'forget' && confirm(`Forget what ${s.name}'s home looks like? You'll teach it again from the tank page.`)) {
          await send(`/api/spotlights/${s.id}/examples`, 'DELETE'); route();
        }
        if (e.target.dataset.a === 'rm' && confirm(`Stop watching ${s.name}? Past moments stay.`)) {
          await send(`/api/spotlights/${s.id}`, 'DELETE'); route();
        }
      });
      $('#slist', spotCard).append(row);
    }
    if (!layout.spotlights.length) $('#slist', spotCard).append(el('<div class="dim">No one yet.</div>'));
    $('#add', spotCard).onclick = () => attempt(async () => {
      const name = $('#newname', spotCard).value.trim();
      if (!name) return flash('Give it a name first');
      await send(`/api/tanks/${t.id}/spotlights`, 'POST', { name });
      location.hash = `#draw/${t.id}`;
    });
    view.append(spotCard);
  } else if (tanks.length) {
    view.append(el('<div class="banner">The camera isn\'t on a tank it recognises, so boxes can\'t be drawn right now. Use "The camera is on this one" above, or set up a new tank.</div>'));
  }

  const n = notify;
  const notifyCard = el(`<div class="card"><h3>Phone notifications</h3>
    <p class="dim" style="margin:6px 0 10px">Install the free <b>ntfy</b> app, tap +, and subscribe to this topic:</p>
    <div class="row"><code class="num grow" style="word-break:break-all">${esc(n.topic)}</code><button class="btn small" id="copy">Copy</button></div>
    <div style="margin-top:10px">
      <div class="toggle"><span>Notifications on</span><input type="checkbox" data-k="enabled"></div>
      <div class="toggle"><span>Morning summary <span class="dim">(${esc(n.morning_at)})</span></span><input type="checkbox" data-k="morning"></div>
      <div class="toggle"><span>If BubbleCam itself stops working</span><input type="checkbox" data-k="system"></div>
      <div class="toggle"><span>When a spotlighted animal comes out <span class="dim">(at most once an hour each)</span></span><input type="checkbox" data-k="on_emerge"></div>
      <div class="toggle"><span>Bursts of activity</span><input type="checkbox" data-k="on_burst"></div>
    </div>
    <div class="row" style="margin-top:8px"><button class="btn small" id="test">Send a test</button><button class="btn small" id="morning">Send last night's summary</button>
      <span class="dim" id="nmsg">${n.last_error ? 'Last send failed: ' + esc(n.last_error) : ''}</span></div>
    <h3 style="margin-top:18px">If the whole Pi goes down</h3>
    <p class="dim" style="margin:6px 0 10px">A switched off Pi can't send a message. A free outside service like Healthchecks.io can: BubbleCam pings it every few minutes, and it tells you when the pings stop. Paste its ping address here.</p>
    <div class="row"><input type="url" class="grow" id="checkin" placeholder="https://hc-ping.com/..." value="${esc(n.checkin_url || '')}"><button class="btn small" id="savecheck">Save</button></div>
    </div>`);
  for (const box of $$('[data-k]', notifyCard)) {
    box.checked = !!n[box.dataset.k];
    box.onchange = () => attempt(() => send('/api/notify', 'PUT', { [box.dataset.k]: box.checked }));
  }
  $('#copy', notifyCard).onclick = () => navigator.clipboard?.writeText(n.topic).then(() => flash('Copied'), () => flash('Copy it by hand'));
  $('#test', notifyCard).onclick = () => attempt(async () => { await send('/api/notify/test', 'POST'); flash('Sent. Check your phone.'); });
  $('#morning', notifyCard).onclick = () => attempt(async () => { await send('/api/notify/morning', 'POST'); flash('Sent'); });
  $('#savecheck', notifyCard).onclick = () => attempt(async () => { await send('/api/notify', 'PUT', { checkin_url: $('#checkin', notifyCard).value }); flash('Saved'); });
  view.append(notifyCard);

  view.append(el('<a class="btn" href="#advanced" style="justify-self:start">Advanced settings and system details</a>'));
}

// Drawing boxes on the live picture

async function pageDraw(arg) {
  const tankId = +arg;
  const layout = await api(`/api/tanks/${tankId}/layout`);
  setTitle(`Boxes: ${layout.name}`);
  const st = await api('/api/status');
  if (!st.tank || st.tank.id !== tankId) {
    view.append(el('<div class="banner">The camera is looking at a different tank right now. Boxes are drawn on the live picture, so point it at this tank first.</div>'));
    return;
  }
  // Modes: each animal's areas, each animal's home, and the ignore areas.
  const modes = [];
  for (const s of layout.spotlights) {
    modes.push({ key: `a${s.id}`, label: `Where ${s.name} hangs out`, color: '--accent', get: () => s.areas, set: v => { s.areas = v; } });
    modes.push({ key: `h${s.id}`, label: `${s.name}'s home (one box)`, color: '--warm', single: true,
      get: () => s.home ? [s.home] : [], set: v => { s.home = v[0] || null; } });
  }
  modes.push({ key: 'ignore', label: 'Ignore: bubbles, filter flow, swaying plants', color: '--dim',
    get: () => layout.settings.ignore, set: v => { layout.settings.ignore = v; } });
  let mode = modes[0];
  let selected = -1;

  const card = el(`<div class="card" style="display:grid;gap:12px">
    <label class="field">What are you drawing?<select id="mode">${modes.map(m => `<option value="${m.key}">${esc(m.label)}</option>`).join('')}</select></label>
    <div class="drawbox"><img alt="current picture"><canvas></canvas></div>
    <p class="dim" style="margin:0">Drag on the picture to draw a box. Tap a box to select it.</p>
    <div class="row"><button class="btn small" id="rm" disabled>Remove selected box</button><button class="btn small" id="pic">New picture</button>
      <span class="grow"></span><button class="btn primary" id="save">Save</button></div></div>`);
  view.append(card);
  const img = $('img', card), canvas = $('canvas', card);
  img.src = `/api/snapshot.jpg?width=1280&t=${Date.now()}`;
  img.onload = render;
  $('#pic', card).onclick = () => { img.src = `/api/snapshot.jpg?width=1280&t=${Date.now()}`; };
  $('#mode', card).onchange = e => { mode = modes.find(m => m.key === e.target.value); selected = -1; render(); };

  let drag = null;
  function render() {
    const r = canvas.getBoundingClientRect(), dpr = devicePixelRatio || 1;
    canvas.width = r.width * dpr; canvas.height = r.height * dpr;
    const g = canvas.getContext('2d'); g.scale(dpr, dpr); g.font = '12px ' + css('--font');
    for (const m of modes) {
      const boxes = m.get();
      const mine = m === mode;
      boxes.forEach((b, i) => {
        g.globalAlpha = mine ? 1 : .35;
        g.strokeStyle = css(m.color); g.lineWidth = mine && i === selected ? 4 : 2;
        g.fillStyle = m.key === 'ignore' ? 'rgba(0,0,0,.45)' : 'rgba(0,0,0,.08)';
        const x = b.x * r.width, y = b.y * r.height, w = b.w * r.width, h = b.h * r.height;
        g.fillRect(x, y, w, h); g.strokeRect(x, y, w, h);
        if (mine) { g.fillStyle = css(m.color); g.fillText(m.label.split(':')[0].replace(' (one box)', ''), x + 4, y + 14); }
      });
    }
    g.globalAlpha = 1;
    if (drag && drag.w > 0) { g.strokeStyle = css(mode.color); g.setLineDash([5, 4]); g.lineWidth = 2;
      g.strokeRect(drag.x * r.width, drag.y * r.height, drag.w * r.width, drag.h * r.height); g.setLineDash([]); }
    $('#rm', card).disabled = selected < 0;
  }
  const rel = e => { const r = canvas.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]; };
  canvas.onpointerdown = e => { canvas.setPointerCapture(e.pointerId); const [x, y] = rel(e); drag = { x0: x, y0: y, x, y, w: 0, h: 0 }; };
  canvas.onpointermove = e => { if (!drag) return; const [x, y] = rel(e);
    Object.assign(drag, { x: Math.min(x, drag.x0), y: Math.min(y, drag.y0), w: Math.abs(x - drag.x0), h: Math.abs(y - drag.y0) }); render(); };
  canvas.onpointerup = () => {
    if (!drag) return;
    if (drag.w > 0.015 && drag.h > 0.015) {
      const box = { x: drag.x, y: drag.y, w: drag.w, h: drag.h };
      mode.set(mode.single ? [box] : [...mode.get(), box]);
      selected = mode.get().length - 1;
    } else {
      selected = mode.get().findIndex(b => drag.x0 >= b.x && drag.x0 <= b.x + b.w && drag.y0 >= b.y && drag.y0 <= b.y + b.h);
    }
    drag = null; render();
  };
  $('#rm', card).onclick = () => { mode.set(mode.get().filter((_, i) => i !== selected)); selected = -1; render(); };
  $('#save', card).onclick = () => attempt(async () => {
    await send(`/api/tanks/${tankId}/layout`, 'PUT', {
      ignore: layout.settings.ignore,
      spotlights: layout.spotlights.map(s => ({ id: s.id, areas: s.areas, home: s.home })),
    });
    flash('Saved');
    location.hash = `#tank/${tankId}`;
  });
  const onResize = () => render();
  addEventListener('resize', onResize);
  return { leave: () => removeEventListener('resize', onResize) };
}

// Advanced

async function pageAdvanced() {
  setTitle('Advanced');
  const [adv, tanks] = await Promise.all([api('/api/advanced'), api('/api/tanks')]);
  for (const t of tanks) {
    const layout = await api(`/api/tanks/${t.id}/layout`);
    const s = layout.settings;
    const card = el(`<div class="card"><h3>${esc(t.name)}: sensitivity</h3>
      <p class="dim" style="margin:6px 0 10px">Calmer means fewer moments and fewer mistakes; eager catches more, including more false ones.
      If you're marking lots of moments as wrong, go calmer.</p>
      <label class="field">Spotlighted animals: <b data-v="spot_sensitivity"></b><input type="range" min="1" max="5" step="1" data-k="spot_sensitivity"></label>
      <label class="field" style="margin-top:10px">Bursts of activity: <b data-v="burst_sensitivity"></b><input type="range" min="1" max="5" step="1" data-k="burst_sensitivity"></label>
      <div class="toggle"><span>Notice feeding by itself</span><input type="checkbox" data-k="feeding_detection"></div></div>`);
    const word = v => ['', 'Very calm', 'Calm', 'Normal', 'Eager', 'Very eager'][v];
    for (const input of $$('[data-k]', card)) {
      const k = input.dataset.k;
      if (input.type === 'checkbox') input.checked = !!s[k]; else { input.value = s[k]; $(`[data-v="${k}"]`, card).textContent = word(s[k]); }
      input.oninput = () => { if (input.type !== 'checkbox') $(`[data-v="${k}"]`, card).textContent = word(+input.value); };
      input.onchange = () => attempt(async () => {
        await send(`/api/tanks/${t.id}`, 'PATCH', { settings: { [k]: input.type === 'checkbox' ? input.checked : +input.value } });
        flash('Saved');
      });
    }
    view.append(card);
  }

  const fa = adv.false_alarms;
  const kindName = k => ({ spot: 'Spotlight moments', burst: 'Bursts', feeding: 'Feedings noticed' }[k] || k);
  view.append(el(`<div class="card scroll-x"><h3 style="margin-bottom:8px">How often it's wrong (last 30 days)</h3>
    <table><thead><tr><th>Tank</th><th>Kind</th><th>Marked wrong</th><th>Total</th></tr></thead><tbody>
    ${fa.map(r => `<tr><td>${esc(r.tank || '')}</td><td>${kindName(r.kind)}</td><td>${r.wrong}</td><td>${r.total}</td></tr>`).join('') || '<tr><td colspan="4" class="dim">Nothing yet.</td></tr>'}
    </tbody></table></div>`));

  const s = adv.storage;
  view.append(el(`<div class="card"><h3 style="margin-bottom:8px">Storage</h3>
    <table><tbody>
    <tr><td>Raw footage</td><td class="num">${s.raw_gb} GB</td><td class="dim">oldest ${s.oldest_raw ? dayTime(s.oldest_raw) : 'none'}; the oldest goes first when space runs low</td></tr>
    <tr><td>Moments</td><td class="num">${s.moments_gb} GB</td><td class="dim">kept ${s.clip_days} days; starred or tagged ones forever</td></tr>
    <tr><td>Timelapses</td><td class="num">${s.timelapses_gb} GB</td><td class="dim">kept ${s.timelapse_days} days; starred ones forever</td></tr>
    </tbody></table></div>`));

  const v = adv.view;
  const viewWords = { ok: 'Steady', adjusted: 'Shifted a little; boxes adjusted to match', moved_far: 'Moved a lot; boxes adjusted to match', unknown: "Doesn't recognise the view", moving: 'Being moved', starting: 'Starting up' };
  view.append(el(`<div class="card"><h3 style="margin-bottom:8px">Camera</h3><table><tbody>
    <tr><td>Looking at</td><td>${esc((tanks.find(t => t.id === v.tank_id) || {}).name || 'nothing it recognises')}</td></tr>
    <tr><td>Position</td><td>${esc(viewWords[v.status] || v.status)}</td></tr>
    <tr><td>Match strength</td><td class="num">${v.inliers} points</td></tr>
    <tr><td>Device</td><td class="num">${esc(adv.camera.device)}, ${esc(adv.camera.size)} at ${adv.camera.fps} fps</td></tr>
    <tr><td>Exposure / gain</td><td class="num">${adv.camera.exposure} / ${adv.camera.gain}</td></tr>
    <tr><td>Video encoder</td><td class="num">${esc(adv.encoder || 'off')}</td></tr>
    <tr><td>Frames dropped</td><td class="num">${adv.dropped_frames}</td></tr>
    </tbody></table><p class="dim">Exposure, gain and other hardware settings live in config.yaml on the Pi.</p></div>`));

  view.append(el(`<div class="card scroll-x"><h3 style="margin-bottom:8px">System history</h3><table><thead><tr><th>What</th><th>From</th><th>Until</th></tr></thead><tbody>
    ${adv.health_log.map(h => `<tr><td>${esc(h.message)}</td><td>${dayTime(h.start_ts)}</td><td>${h.end_ts ? dayTime(h.end_ts) : 'still going'}</td></tr>`).join('') || '<tr><td colspan="3" class="dim">No problems recorded.</td></tr>'}
    </tbody></table></div>`));
}

// Start

pollStatus().then(route);
