'use strict';
/* The Accountability Playground page. No framework, no build step.
   Talks to /api/* (server) and to the sandboxed viewer iframe (postMessage). */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const H = { 'Content-Type': 'application/json', 'X-Playground': '1' };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtUsd = (x) => (x || 0) < 0.01 ? `$${(x || 0).toFixed(4)}` : `$${(x || 0).toFixed(3)}`;
const fmtS = (ms) => ms == null ? '' : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;

const EXAMPLES = [
  ['Red PBR sphere', 'A red PBR sphere on a grey ground plane with a named viewpoint'],
  ['Solar system', 'A sun, an earth and a moon; the earth orbits the sun every 20 seconds and the moon orbits the earth'],
  ['Click to change', 'A blue box that turns gold when you click it, and back to blue when you click again'],
  ['The Drillfield', 'The Virginia Tech Drillfield: a green oval lawn crossed by two paths, eight trees, and a grey stone hall at one end'],
  ['DNA helix', 'A slowly spinning DNA double helix of 16 base pairs with colored rungs'],
  ['Desert canopy', 'A shade canopy of 12 tilting louvers over a courtyard at noon, with a warm directional sun'],
  ['Splat spiral (4.1)', 'An X3D 4.1 scene with a GaussianSplats cloud of 120 glowing points arranged in a rising spiral', '4.1'],
  ['Stick figure', 'A stick figure made of cylinders and spheres that waves one arm'],
];

const HEAD = (v = '4.0', profile = 'Immersive') => `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE X3D PUBLIC "ISO//Web3D//DTD X3D ${v}//EN" "https://www.web3d.org/specifications/x3d-${v}.dtd">
<X3D profile='${profile}' version='${v}' xmlns:xsd='http://www.w3.org/2001/XMLSchema-instance' xsd:noNamespaceSchemaLocation='https://www.web3d.org/specifications/x3d-${v}.xsd'>
  <head>
    <meta name='title' content='Playground starter'/>
    <meta name='creator' content='X3D Accountability Playground'/>
  </head>`;

const STARTER = `${HEAD()}
  <Scene>
    <Background skyColor='0.05 0.06 0.1'/>
    <NavigationInfo type='"EXAMINE" "ANY"'/>
    <Viewpoint DEF='Front' description='Front view' position='0 1.2 7' orientation='1 0 0 -0.12'/>
    <DirectionalLight direction='-0.4 -1 -0.6' intensity='0.9'/>
    <Transform translation='0 -0.6 0'>
      <Shape>
        <Appearance><Material diffuseColor='0.35 0.37 0.42'/></Appearance>
        <Box size='8 0.1 8'/>
      </Shape>
    </Transform>
    <Transform DEF='Spinner' translation='0 0.6 0'>
      <TouchSensor DEF='Touch' description='click to restart the spin'/>
      <Shape>
        <Appearance><Material DEF='Paint' diffuseColor='0.15 0.45 1' specularColor='0.6 0.6 0.6' shininess='0.4'/></Appearance>
        <Box size='1.4 1.4 1.4'/>
      </Shape>
    </Transform>
    <TimeSensor DEF='Clock' cycleInterval='6' loop='true'/>
    <OrientationInterpolator DEF='Turn' key='0 0.5 1' keyValue='0 1 0 0 0 1 0 3.14159 0 1 0 6.28318'/>
    <ROUTE fromNode='Clock' fromField='fraction_changed' toNode='Turn' toField='set_fraction'/>
    <ROUTE fromNode='Turn' fromField='value_changed' toNode='Spinner' toField='set_rotation'/>
    <ROUTE fromNode='Touch' fromField='touchTime' toNode='Clock' toField='set_startTime'/>
  </Scene>
</X3D>`;

const BROKEN = [
  ['A misspelled ROUTE field', STARTER.replace("toField='set_rotation'", "toField='set_rotaton'")],
  ['An X3D 4.1 node in a 4.0 file', STARTER.replace("<Background", "<EnvironmentLight intensity='0.4'/>\n    <Background")],
  ['A field that does not exist', STARTER.replace("diffuseColor='0.15 0.45 1'", "diffuseColor='0.15 0.45 1' colour='0.15 0.45 1'")],
  ['Key and keyValue disagree', STARTER.replace("key='0 0.5 1'", "key='0 0.25 0.5 1'")],
  ['Profile too low for the animation', STARTER.replace("profile='Immersive'", "profile='Interchange'")],
  ['Silent fault: valid, but invisible', STARTER.replace(/<Material /g, "<Material transparency='1' ")],
  ['Silent fault: the camera looks away', STARTER.replace("orientation='1 0 0 -0.12'", "orientation='0 1 0 3.14159'")],
];

const state = {
  me: null, mode: 'prompt', writer: 'a', repairs: 2, version: '4.0',
  running: false, run: null, steps: new Map(), stepOrder: [], judge: null, final: null,
  scene: STARTER, viewSeq: 0, viewPending: new Map(), viewerReady: false, queuedView: null,
  breakScore: { validator: [0, 0], browser: [0, 0] }, lastPrompt: '', arena: null, didVisibleRepair: false,
};

/* ---------------------------------------------------------------- boot */

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: 'same-origin', ...opts, headers: { ...H, ...(opts.headers || {}) } });
  let data = null;
  try { data = await res.json(); } catch { data = {}; }
  if (res.status === 401) { location.href = '/'; throw new Error('signed out'); }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, ok = false) {
  const t = $('#toast');
  t.textContent = msg; t.classList.toggle('ok', ok); t.classList.remove('hidden');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add('hidden'), ok ? 3500 : 7000);
}

async function loadMe() {
  const me = await api('/api/me');
  state.me = me;
  $('#b-validator').textContent = me.validator;
  $('#b-xite').textContent = me.xite;
  $('#who').textContent = me.me.display;
  $('#admin-link').classList.toggle('hidden', !me.admin);
  $('#pmax').textContent = me.max_prompt_chars;
  const L = me.limits;
  $('#m-hour').textContent = `hour ${L.runs_hour}/${L.runs_hour_limit}`;
  $('#m-day').textContent = `day ${L.runs_day}/${L.runs_day_limit}`;
  $('#m-hour-bar').style.width = `${Math.min(100, 100 * L.runs_hour / Math.max(1, L.runs_hour_limit))}%`;
  $('#m-day-bar').style.width = `${Math.min(100, 100 * L.runs_day / Math.max(1, L.runs_day_limit))}%`;
  $('#paused-note').textContent = me.paused ? 'The owner has paused model runs. Validation and Break it still work.' : '';
  renderWriters();
  renderOps();
}

function renderWriters() {
  const box = $('#writers');
  box.innerHTML = '';
  for (const w of state.me.writers) {
    const el = document.createElement('div');
    el.className = 'writer' + (w.code === state.writer ? ' on' : '') + (w.enabled ? '' : ' off');
    el.innerHTML = `<b>${esc(w.label)}</b><span>${esc(w.blurb)}${w.enabled ? '' : ' · off'}</span>`;
    if (w.enabled) el.onclick = () => { state.writer = w.code; renderWriters(); };
    box.appendChild(el);
  }
  if (!state.me.writers.some((w) => w.code === state.writer && w.enabled)) {
    const first = state.me.writers.find((w) => w.enabled);
    if (first) { state.writer = first.code; renderWriters(); }
  }
}

function writerLabel(code) {
  const w = state.me?.writers.find((x) => x.code === code);
  return w ? w.label : code === 'validator' ? 'Validator' : code === 'person' ? 'You' : code;
}

function setupControls() {
  for (const [label, text, ver] of EXAMPLES) {
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = () => { $('#prompt').value = text; setVersion(ver || '4.0'); updateChars(); };
    $('#examples').appendChild(b);
  }
  BROKEN.forEach(([label], i) => {
    const o = document.createElement('option'); o.value = String(i); o.textContent = label; $('#paste-example').appendChild(o);
  });
  $('#paste-example').onchange = (e) => { if (e.target.value !== '') $('#paste').value = BROKEN[+e.target.value][1]; };
  $$('.tabs button').forEach((b) => b.onclick = () => setMode(b.dataset.mode));
  $$('#repairs button').forEach((b) => b.onclick = () => { state.repairs = +b.dataset.v; $$('#repairs button').forEach((x) => x.classList.toggle('on', x === b)); });
  $$('#version button').forEach((b) => b.onclick = () => setVersion(b.dataset.v));
  $('#prompt').addEventListener('input', updateChars);
  $('#prompt').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) runClicked(); });
  $('#run-btn').onclick = runClicked;
  $('#validate-btn').onclick = validateOnly;
  $('#signout').onclick = async () => { await fetch('/auth/signout', { method: 'POST', headers: H }); location.href = '/'; };
  $('#dl-x3d').onclick = () => download(`playground-${state.run?.run_id || 'scene'}.x3d`, state.final?.x3d || state.scene, 'model/x3d+xml');
  $('#dl-report').onclick = () => download(`playground-${state.run?.run_id || 'run'}-report.json`, JSON.stringify(reportBundle(), null, 2), 'application/json');
  $('#copy-receipt').onclick = async () => { await navigator.clipboard.writeText(receiptText()); toast('Receipt copied.', true); };
  $('#fix-visible').onclick = () => visibilityRepair(true);
  updateChars();
}

function setVersion(v) { state.version = v; $$('#version button').forEach((x) => x.classList.toggle('on', x.dataset.v === v)); }
function updateChars() { $('#pchars').textContent = $('#prompt').value.length; }

function setMode(mode) {
  state.mode = mode;
  $$('.tabs button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
  $$('[data-pane]').forEach((p) => p.classList.toggle('hidden', p.dataset.pane !== mode));
  $('#run-btn').textContent = mode === 'paste' ? 'Validate, then repair with the writer' : mode === 'break' ? 'Run the loop on the broken scene' : 'Run the loop';
  if (mode === 'paste' && !$('#paste').value.trim()) $('#paste').value = state.final?.x3d || state.scene;
}

/* ---------------------------------------------------------------- the loop diagram */

function st(id, cls, sub) {
  const g = $(`#s-${id}`);
  if (!g) return;
  g.classList.remove('active', 'pass', 'fail', 'done');
  if (cls) g.classList.add(cls);
  if (sub != null) { const s = $(`#s-${id}-sub`); if (s) s.textContent = sub; }
}
function edge(id, on) { $(`#e-${id}`)?.classList.toggle('lit', !!on); }
function resetDiagram() {
  ['write', 'validate', 'fix', 'repair', 'browser', 'judge'].forEach((s) => st(s, null));
  st('write', null, 'model'); st('validate', null, 'XML · XSD · semantic'); st('fix', null, 'exact, mechanical');
  st('repair', null, 'from the report'); st('browser', null, 'visible?');
  $('#s-judge-t').textContent = 'Judge alone (no validator)';
  ['wv', 'vf', 'fr', 'rv', 'vb', 'wj'].forEach((e) => edge(e, false));
  $('#l-vf').textContent = '';
}

/* ---------------------------------------------------------------- steps */

const KIND_TITLE = {
  write: (s) => `${writerLabel(s.by)} writes draft ${s.n}`,
  source: () => 'Your scene',
  mutation: (s) => `Broken on purpose: ${s.what || ''}`,
  autofix: (s) => `Validator applies ${s.changes?.length ?? ''} exact fix${s.changes?.length === 1 ? '' : 'es'}`,
  repair: (s) => `${writerLabel(s.by)} repairs from the report`,
  'visible-repair': (s) => `${writerLabel(s.by)} repairs for visibility`,
};

function stepCard(n) {
  let s = state.steps.get(n);
  if (!s) {
    s = { n, kind: 'write', by: state.writer, open: true };
    state.steps.set(n, s); state.stepOrder.push(n);
    for (const m of state.stepOrder) if (m !== n) state.steps.get(m).open = false;
  }
  return s;
}

function drawSteps() {
  const box = $('#steps');
  if (!state.stepOrder.length) { box.innerHTML = '<div class="empty">Run the loop to see each draft, the exact defects, and each repair.</div>'; return; }
  box.innerHTML = '';
  for (const n of state.stepOrder) box.appendChild(renderStep(state.steps.get(n)));
}

function countsHtml(r) {
  if (!r) return '<span class="pill wait">checking</span>';
  const c = r.counts;
  let h = `<span class="pill ${r.verdict}">${r.verdict.toUpperCase()}</span>`;
  if (c.xml) h += ` <span class="cnt xml">XML ${c.xml}</span>`;
  if (c.xsd) h += ` <span class="cnt xsd">XSD ${c.xsd}</span>`;
  if (c.semantic) h += ` <span class="cnt sem">semantic ${c.semantic}</span>`;
  if (c.warnings) h += ` <span class="cnt wrn">warn ${c.warnings}</span>`;
  return h;
}

function renderStep(s) {
  const el = document.createElement('div');
  const by = s.by === 'validator' ? 'validator' : s.by === 'person' ? 'person' : 'model';
  el.className = `step by-${by}`;
  const title = (KIND_TITLE[s.kind] || (() => s.kind))(s);
  const meta = [];
  if (s.tokens_out) meta.push(`${s.tokens_in}→${s.tokens_out} tok`);
  if (s.ms) meta.push(fmtS(s.ms));
  if (s.usd) meta.push(fmtUsd(s.usd));
  if (s.report) meta.push(`checked in ${fmtS(s.report.ms.total)}`);
  if (s.changes?.length) meta.push(`<span class="cnt fix">${s.changes.length} fixed by the validator</span>`);
  el.innerHTML = `<div class="hd"><span class="num">${s.n}</span><b class="grow">${esc(title)}</b>
     <span class="small muted">${meta.join(' · ')}</span> ${s.x3d || s.live ? countsHtml(s.report) : ''}</div>`;
  el.querySelector('.hd').onclick = () => { s.open = !s.open; drawSteps(); };
  if (!s.open) return el;
  const bd = document.createElement('div');
  bd.className = 'bd';
  if (s.error) bd.innerHTML += `<div class="small" style="color:var(--fail)">${esc(s.error)}</div>`;
  if (s.x3d == null && s.live != null) {
    bd.innerHTML += `<div class="code"><div class="live">${esc(s.live.slice(-6000))}</div></div>`;
  } else if (s.x3d != null) {
    const prev = prevScene(s.n);
    const tools = document.createElement('div');
    tools.className = 'row small';
    tools.style.marginBottom = '6px';
    tools.innerHTML = `<button class="chip-btn" data-a="view">Show this draft in the viewer</button>
      ${prev != null ? `<button class="chip-btn" data-a="diff">${s.diff ? 'Show the code' : 'Show what changed'}</button>` : ''}
      <button class="chip-btn" data-a="copy">Copy</button>
      <span class="faint grow" style="text-align:right">${s.report ? `${s.report.stats.nodes} nodes · ${s.report.stats.routes} ROUTEs · ${s.report.stats.bytes.toLocaleString()} B · sha256 ${s.report.sha256.slice(0, 10)}` : ''}</span>`;
    tools.querySelector('[data-a=view]').onclick = (e) => { e.stopPropagation(); showInViewer(s.x3d, { step: s.n, label: `step ${s.n}` }); };
    tools.querySelector('[data-a=copy]').onclick = async (e) => { e.stopPropagation(); await navigator.clipboard.writeText(s.x3d); toast('Copied.', true); };
    const d = tools.querySelector('[data-a=diff]');
    if (d) d.onclick = (e) => { e.stopPropagation(); s.diff = !s.diff; drawSteps(); };
    bd.appendChild(tools);
    bd.appendChild(s.diff && prev != null ? diffView(prev, s.x3d) : codeView(s.x3d, s.report));
  }
  if (s.changes?.length) {
    bd.innerHTML += `<table class="f"><tr><th>validator fix</th><th>from → to</th><th>why</th></tr>${s.changes.map((c) =>
      `<tr><td class="layer" style="color:var(--fix)">${esc(c.kind)}${c.name ? ' ' + esc(c.name) : ''}</td><td class="mono small">${esc(short(c.from))} → ${esc(short(c.to))}</td><td>${esc(c.reason || '')}</td></tr>`).join('')}</table>`;
  }
  if (s.report) bd.appendChild(findingsTable(s.report));
  el.appendChild(bd);
  return el;
}

function short(v) { const t = v == null ? '—' : String(v); return t.length > 60 ? t.slice(0, 57) + '…' : t; }

function prevScene(n) {
  const i = state.stepOrder.indexOf(n);
  for (let k = i - 1; k >= 0; k--) { const p = state.steps.get(state.stepOrder[k]); if (p.x3d != null) return p.x3d; }
  return null;
}

function codeView(text, report) {
  const box = document.createElement('div');
  box.className = 'code';
  const marks = new Map();
  for (const f of report?.findings || []) if (f.level === 'error' && f.line) { if (!marks.has(f.line)) marks.set(f.line, f); }
  const lines = text.split('\n');
  let html = '<table>';
  lines.forEach((ln, i) => {
    const f = marks.get(i + 1);
    const dot = f ? `<i style="background:var(--${f.layer === 'semantic' ? 'sem' : f.layer})" title="${esc(f.message)}"></i>` : '';
    html += `<tr class="${f ? 'e-' + f.layer : ''}" id="L${i + 1}"><td class="mk">${dot}</td><td class="ln">${i + 1}</td><td class="src">${esc(ln)}</td></tr>`;
  });
  box.innerHTML = html + '</table>';
  const first = [...marks.keys()].sort((a, b) => a - b)[0];
  if (first) requestAnimationFrame(() => { const row = box.querySelector(`#L${first}`); if (row) box.scrollTop = Math.max(0, row.offsetTop - 60); });
  return box;
}

function diffView(a, b) {
  const A = a.split('\n'), B = b.split('\n');
  const box = document.createElement('div');
  box.className = 'code diffbox';
  if (A.length * B.length > 600000) { box.innerHTML = '<div class="live">Too large to diff here; download both drafts.</div>'; return box; }
  const m = A.length, n = B.length, dp = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
  for (let i = m - 1; i >= 0; i--) for (let j = n - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  let i = 0, j = 0, html = '<table>', added = 0, removed = 0;
  while (i < m || j < n) {
    if (i < m && j < n && A[i] === B[j]) { html += `<tr><td class="mk"></td><td class="ln">${j + 1}</td><td class="src">${esc(B[j])}</td></tr>`; i++; j++; }
    else if (j < n && (i >= m || dp[i][j + 1] >= dp[i + 1][j])) { html += `<tr class="add"><td class="mk">+</td><td class="ln">${j + 1}</td><td class="src">${esc(B[j])}</td></tr>`; j++; added++; }
    else { html += `<tr class="e-xml"><td class="mk">−</td><td class="ln"></td><td class="src">${esc(A[i])}</td></tr>`; i++; removed++; }
  }
  box.innerHTML = `<div class="small muted" style="padding:6px 10px">+${added} lines, −${removed} lines</div>` + html + '</table>';
  return box;
}

function findingsTable(r) {
  const wrap = document.createElement('div');
  const rows = r.findings.filter((f) => f.level !== 'info' || f.check.endsWith('-fix'));
  if (!rows.length) {
    wrap.innerHTML = `<div class="small" style="margin-top:8px;color:var(--pass)">No defects: well-formed, schema-valid for X3D ${esc(r.schema_version)}, no semantic errors.</div>`;
    return wrap;
  }
  wrap.innerHTML = `<table class="f"><tr><th>layer</th><th>line</th><th>exactly what is wrong</th></tr>${rows.map((f) => `
    <tr><td class="layer L-${f.layer}">${esc(f.layer)}${f.level !== 'error' ? ' · ' + esc(f.level) : ''}<div class="faint">${esc(f.check)}</div></td>
      <td class="mono small">${f.line ? (f.located ? '≈' : '') + f.line : '—'}</td>
      <td>${esc(f.message)}${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}</td></tr>`).join('')}</table>
    ${(r.notes || []).map((n) => `<div class="hint">note: ${esc(n)}</div>`).join('')}
    ${r.auto_repair?.available ? `<div class="hint" style="color:var(--fix)">The validator can fix ${r.auto_repair.changes.length} of these mechanically${r.auto_repair.unfixable?.length ? `; ${r.auto_repair.unfixable.length} need a decision` : ''}.</div>` : ''}
    <div class="tiny faint" style="margin-top:4px">≈ marks a line the page located from the message; the semantic checks do not report lines.</div>`;
  return wrap;
}

/* ---------------------------------------------------------------- running */

function runClicked() {
  if (state.running) return;
  if (state.mode === 'prompt') {
    const prompt = $('#prompt').value.trim();
    if (!prompt) { toast('Type a prompt, or pick an example.'); return; }
    state.lastPrompt = prompt;
    startRun({ prompt });
  } else if (state.mode === 'paste') {
    const x3d = $('#paste').value;
    if (!x3d.trim()) { toast('Paste a scene first.'); return; }
    state.lastPrompt = 'Repair this X3D scene so it is valid and shows its content.';
    startRun({ x3d, prompt: state.lastPrompt });
  } else {
    state.lastPrompt = 'Repair this X3D scene so it is valid and shows its content.';
    startRun({ x3d: state.scene, prompt: state.lastPrompt });
  }
}

function resetRun() {
  state.viewSeq++;   // a frame check still running belongs to the old run: drop it
  state.pendingVisible = false;
  state.steps = new Map(); state.stepOrder = []; state.judge = null; state.final = null; state.didVisibleRepair = false;
  resetDiagram(); drawSteps();
  $('#judge-live').innerHTML = 'The live judge appears here after a run.';
  setTile('v-valid', '', '—', 'XML, XSD, semantic'); setTile('v-visible', '', '—', 'pixels that differ from the background');
  $('#fix-visible').classList.add('hidden');
}

async function startRun(extra, opts = {}) {
  if (!opts.keep) { resetRun(); state.stepOffset = 0; }
  else state.stepOffset = state.stepOrder.length ? Math.max(...state.stepOrder) : 0;
  state.running = true;
  $('#run-btn').disabled = true;
  $('#run-btn').textContent = 'Running…';
  const body = {
    writer: state.writer, max_repairs: state.repairs, version: state.version,
    autofix: $('#autofix').checked, judge: $('#judge-on').checked ? '' : 'off', ...extra,
  };
  try {
    const res = await fetch('/api/run', { method: 'POST', headers: H, body: JSON.stringify(body), credentials: 'same-origin' });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.error || `HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
        for (const line of chunk.split('\n')) if (line.startsWith('data: ')) onEvent(JSON.parse(line.slice(6)));
      }
    }
  } catch (e) {
    toast(e.message);
  } finally {
    state.running = false;
    $('#run-btn').disabled = false;
    setMode(state.mode);
    loadMe().catch(() => {});
    loadHistory().catch(() => {});
    // The browser saw nothing while the judge was still answering: repair now.
    if (state.pendingVisible) { state.pendingVisible = false; visibilityRepair(false); }
  }
}

function onEvent(ev) {
  // A visibility repair continues the run on the page: its steps follow on.
  if (state.stepOffset && ev.step) ev = { ...ev, step: ev.step + state.stepOffset };
  switch (ev.t) {
    case 'run': {
      if (state.stepOffset && state.run) ev.parent = state.run.run_id;
      state.run = ev;
      $('#run-id').textContent = `run ${ev.run_id} · ${ev.mode}`;
      if (ev.writer) { st('write', null, ev.writer.label); st('repair', null, ev.writer.label); }
      if (ev.judge) $('#s-judge-t').textContent = `Judge alone: ${ev.judge.label}`;
      else st('judge', 'done');
      if (ev.mode !== 'prompt') st('write', 'done', ev.mode === 'paste' ? 'your scene' : 'visibility');
      break;
    }
    case 'stage': {
      if (ev.kind === 'judge') {
        st('judge', ev.state === 'start' ? 'active' : 'done'); edge('wj', ev.state === 'start');
        if (ev.state === 'start') $('#judge-live').innerHTML = `<span class="muted">${esc(state.run?.judge?.label || 'The judge')} is reading draft 1 with no validator. Its verdict lands here.</span>`;
        break;
      }
      const s = stepCard(ev.step);
      Object.assign(s, { kind: ev.kind, by: ev.by });
      if (ev.state === 'start') {
        s.live = '';
        if (ev.kind === 'write') { st('write', 'active'); edge('wv', true); }
        else { st('repair', 'active'); edge('rv', false); }
      } else if (ev.state === 'error') { s.error = ev.message; st(ev.kind === 'write' ? 'write' : 'repair', 'fail'); }
      drawSteps();
      break;
    }
    case 'delta': {
      const s = stepCard(ev.step);
      s.live = (s.live || '') + ev.text;
      const live = $('#steps .step:last-child .live');
      if (live && state.stepOrder[state.stepOrder.length - 1] === ev.step) {
        live.textContent = s.live.slice(-6000);
        const box = live.parentElement; box.scrollTop = box.scrollHeight;
      } else drawSteps();
      break;
    }
    case 'draft': {
      const s = stepCard(ev.step);
      Object.assign(s, { kind: ev.kind, by: ev.by, x3d: ev.x3d, tokens_in: ev.tokens_in, tokens_out: ev.tokens_out, usd: ev.usd, ms: ev.ms, changes: ev.changes });
      if (ev.kind === 'write') { st('write', 'done'); edge('wv', true); }
      if (ev.kind === 'repair' || ev.kind === 'visible-repair') { st('repair', 'done'); edge('wv', false); }
      if (ev.kind === 'autofix') { st('fix', 'pass', `${ev.changes.length} change${ev.changes.length === 1 ? '' : 's'}`); edge('vf', true); }
      st('validate', 'active'); drawSteps();
      break;
    }
    case 'report': {
      const s = stepCard(ev.step);
      s.report = ev.report;
      const c = ev.report.counts;
      st('validate', ev.report.verdict, ev.report.verdict === 'pass' ? `pass in ${fmtS(ev.report.ms.total)}` : `${c.errors} defect${c.errors === 1 ? '' : 's'}`);
      if (ev.report.verdict === 'fail') {
        edge('rv', true);
        $('#l-vf').textContent = `${c.errors} defects`;
      } else { edge('rv', false); edge('vb', true); }
      setTile('v-valid', ev.report.verdict, ev.report.verdict === 'pass' ? 'VALID' : 'INVALID',
        ev.report.verdict === 'pass' ? `X3D ${ev.report.schema_version} · step ${ev.step}` : `${c.errors} defects at step ${ev.step}`);
      drawSteps();
      break;
    }
    case 'judge': { state.judge = ev; drawJudge(); break; }
    case 'error': { toast(ev.message); break; }
    case 'final': {
      state.final = ev;
      state.scene = ev.x3d || state.scene;
      ['write', 'repair'].forEach((x) => { const g = $(`#s-${x}`); if (g.classList.contains('active')) st(x, 'done'); });
      edge('wv', false); edge('rv', false); edge('vf', false);
      drawReceipt();
      $('#dl-x3d').disabled = $('#dl-report').disabled = $('#copy-receipt').disabled = !ev.x3d;
      if (ev.x3d) showInViewer(ev.x3d, { step: 'final', label: 'final scene', final: true });
      break;
    }
    default: break;
  }
}

function setTile(id, cls, v, d) {
  const t = $(`#${id}`);
  t.classList.remove('pass', 'fail', 'warn');
  if (cls) t.classList.add(cls);
  $('.v', t).textContent = v; $('.d', t).textContent = d;
}

/* ---------------------------------------------------------------- the judge */

function drawJudge() {
  const ev = state.judge;
  const r = ev.result;
  if (r.error) { $('#judge-live').innerHTML = `<span class="muted">${esc(ev.judge.label)}: ${esc(r.error)}</span>`; st('judge', 'fail'); return; }
  const c = r.compare;
  const first = state.steps.get(state.stepOrder[0])?.report;
  const errors = (first?.findings || []).filter((f) => f.level === 'error');
  const caught = new Set(c.caught_index);
  st('judge', c.agree ? 'pass' : 'fail');
  $('#s-judge-t').textContent = `Judge: ${c.judge_verdict} · validator: ${c.validator_verdict}`;
  $('#judge-live').innerHTML = `
    <div class="row" style="margin-bottom:6px"><b>${esc(ev.judge.label)}</b><span class="muted">(${esc(state.me.writers.find((w) => w.code === ev.judge.code)?.blurb || '')}, another family than the writer) reviewed draft 1 with no validator.</span></div>
    <div class="row" style="margin-bottom:8px">
      <span class="pill ${c.agree ? 'pass' : 'fail'}">${c.agree ? 'AGREES' : c.agree === false ? 'DISAGREES' : 'UNPARSED'}</span>
      <span>Judge said <b>${esc(c.judge_verdict)}</b>; the validator says <b>${esc(c.validator_verdict)}</b>.</span>
      <span class="muted">Caught ${c.caught} of ${c.validator_errors} validator errors · missed ${c.missed} · ${c.unmatched_claims} claims the validator does not make · ${fmtS(r.ms)} · ${fmtUsd(r.usd)}</span>
    </div>
    <div class="judge-grid">
      <div><div class="small muted">Validator, exact (${errors.length})</div><ul>${errors.map((f, i) => `<li class="${caught.has(i) ? 'hit' : 'miss'}">${caught.has(i) ? '✓' : '✗'} <span class="L-${f.layer}">${esc(f.layer)}</span> ${f.line ? 'line ' + f.line + ': ' : ''}${esc(short(f.message))}</li>`).join('') || '<li class="hit">no defects</li>'}</ul></div>
      <div><div class="small muted">Judge's claims (${(r.defects || []).length})</div><ul>${(r.defects || []).slice(0, 12).map((d) => `<li>${d.line ? 'line ' + esc(d.line) + ': ' : ''}${esc(short(d.what))}</li>`).join('') || '<li>none: "valid"</li>'}</ul></div>
    </div>
    <div class="tiny faint" style="margin-top:6px">Matching: ${esc(c.method)}. A ✓ means the judge pointed at that defect; it does not mean it named the fix.</div>`;
}

/* ---------------------------------------------------------------- viewer */

function showInViewer(x3d, meta) {
  const id = ++state.viewSeq;
  state.viewPending.set(id, { ...meta, x3d, t0: performance.now() });
  $('#view-ov').textContent = `rendering ${meta.label} in X_ITE ${state.me?.xite || ''}…`;
  $('#view-label').textContent = meta.label;
  st('browser', 'active');
  const msg = { type: 'load', id, x3d };
  if (state.viewerReady) $('#viewer').contentWindow.postMessage(msg, '*');
  else state.queuedView = msg;
}

window.addEventListener('message', (ev) => {
  if (ev.source !== $('#viewer').contentWindow) return;
  const m = ev.data || {};
  if (m.type === 'ready') {
    state.viewerReady = true;
    if (state.queuedView) { $('#viewer').contentWindow.postMessage(state.queuedView, '*'); state.queuedView = null; }
    else showInViewer(state.scene, { step: 'starter', label: 'starter scene' });
    return;
  }
  if (m.type !== 'result') return;
  const meta = state.viewPending.get(m.id);
  if (!meta || m.id !== state.viewSeq) return;
  state.viewPending.delete(m.id);
  onVisible(m, meta);
});

function visibleWords(m) {
  if (m.load_error) return ['fail', 'NOT LOADED', short(m.load_error)];
  const pct = (m.coverage * 100).toFixed(m.coverage < 0.01 ? 3 : 1);
  if (m.status === 'blank') return ['fail', 'BLANK', `${pct}% of pixels differ from the background`];
  if (m.status === 'sparse') return ['warn', 'SPARSE', `${pct}% of pixels: present but tiny or clipped`];
  return ['pass', 'VISIBLE', `${pct}% of the frame · ${m.width}×${m.height}`];
}

function onVisible(m, meta) {
  const [cls, word, detail] = visibleWords(m);
  setTile('v-visible', cls, word, detail);
  st('browser', cls === 'pass' ? 'pass' : 'fail', word.toLowerCase());
  edge('vb', false);
  $('#view-ov').textContent = `${word.toLowerCase()} · ${detail} · ${fmtS(m.ms)}`;
  $('#browser-msgs').innerHTML = (m.messages || []).length ? m.messages.map((x) => `<div>${esc(x)}</div>`).join('') : 'No warnings or errors from X_ITE.';
  if (meta.final && state.run && state.final) {
    state.final.visible = m;
    drawReceipt();
    fetch(`/api/runs/${state.run.run_id}/visible`, {
      method: 'POST', headers: H, credentials: 'same-origin',
      body: JSON.stringify({ ...m, step: 'final', sha256: state.final.summary?.sha256, xite: state.me.xite }),
    }).catch(() => {});
    const bad = cls !== 'pass' || (m.messages || []).some((x) => /error/i.test(x));
    $('#fix-visible').classList.toggle('hidden', !(bad && state.final.verdict === 'pass' && state.run.writer));
    // Only a blank or unloadable frame repairs itself; sparse or noisy ones offer the button.
    if (cls === 'fail' && state.final.verdict === 'pass' && $('#auto-visible').checked && !state.didVisibleRepair && state.run.mode !== 'visible-repair' && state.run.writer) {
      visibilityRepair(false);
    }
  }
  if (meta.breakOp) scoreBreak(meta, m);
}

function visibilityRepair(manual) {
  if (!state.final?.x3d) return;
  if (state.running) { if (!manual) state.pendingVisible = true; return; }
  state.didVisibleRepair = true;
  const v = state.final.visible || {};
  toast(manual ? 'Asking the writer to repair for visibility.' : 'Valid, but the browser sees nothing: closing the loop with one visibility repair.', true);
  startRun({ x3d: state.final.x3d, prompt: state.lastPrompt, visibility: v, parent_run: state.run.run_id, judge: 'off' }, { keep: true });
}

/* ---------------------------------------------------------------- validate only, break it */

async function validateOnly() {
  const x3d = $('#paste').value;
  if (!x3d.trim()) { toast('Paste a scene first.'); return; }
  await validateScene(x3d, { kind: 'source', by: 'person' });
}

async function validateScene(x3d, stepInfo, viewMeta = {}) {
  resetRun();
  state.run = null;
  $('#run-id').textContent = 'validation only · no model';
  st('write', 'done', stepInfo.kind === 'mutation' ? 'mutation' : 'your scene'); st('validate', 'active'); st('judge', 'done');
  const s = stepCard(1);
  Object.assign(s, stepInfo, { x3d });
  drawSteps();
  try {
    const out = await api('/api/validate', { method: 'POST', body: JSON.stringify({ x3d }) });
    onEvent({ t: 'report', step: 1, report: out.report });
    state.final = { x3d, verdict: out.report.verdict, summary: { sha256: out.report.sha256, steps: [] } };
    drawReceipt();
    $('#dl-x3d').disabled = $('#dl-report').disabled = $('#copy-receipt').disabled = false;
    showInViewer(x3d, { step: 1, label: stepInfo.kind === 'mutation' ? 'broken scene' : 'your scene', ...viewMeta, report: out.report });
    return out.report;
  } catch (e) { toast(e.message); return null; }
}

function renderOps() {
  const box = $('#ops');
  box.innerHTML = '';
  for (const op of state.me.mutations) {
    const b = document.createElement('button');
    b.className = 'op' + (op.caught_by === 'browser' ? ' br' : op.caught_by === 'warning' ? ' wa' : '');
    const what = { browser: 'stays valid: only the browser sees it', warning: 'legal X3D: the validator warns', validator: 'the validator should catch it' };
    b.innerHTML = `${esc(op.op)}<small>${what[op.caught_by] || what.validator}</small>`;
    b.onclick = () => breakIt(op.op);
    box.appendChild(b);
  }
}

async function breakIt(op) {
  const base = state.final?.verdict === 'pass' && state.final.x3d ? state.final.x3d : STARTER;
  let out;
  try { out = await api('/api/mutate', { method: 'POST', body: JSON.stringify({ x3d: base, op, seed: Date.now() % 1000 }) }); }
  catch (e) { toast(e.message); return; }
  if (!out.applied) { toast(`Not applicable to this scene: ${out.reason}.`); return; }
  state.scene = out.x3d;
  const report = await validateScene(out.x3d, { kind: 'mutation', by: 'person', what: out.what }, { breakOp: op, caughtBy: out.caught_by, what: out.what });
  if (report) state.lastBreak = { op, caught_by: out.caught_by, verdict: report.verdict };
}

function scoreBreak(meta, m) {
  const report = meta.report;
  const warned = meta.caughtBy === 'warning' && report.counts.warnings > 0;
  const validatorCaught = report.verdict === 'fail' || warned;
  const browserCaught = !!m.load_error || m.status !== 'ok';
  const key = meta.caughtBy === 'browser' ? 'browser' : 'validator';
  state.breakScore[key][1] += 1;
  if ((key === 'validator' && validatorCaught) || (key === 'browser' && browserCaught)) state.breakScore[key][0] += 1;
  const S = state.breakScore;
  $('#break-score').innerHTML = `Caught this session: validator <b>${S.validator[0]} of ${S.validator[1]}</b>, browser <b>${S.browser[0]} of ${S.browser[1]}</b>.`;
  const line = key === 'validator'
    ? (warned ? `"${meta.what}" is legal X3D, so the scene stays valid, but the validator warns about it exactly. The browser says ${m.status}.`
      : validatorCaught ? `The validator caught "${meta.what}" with ${report.counts.errors} exact defect(s).` : `The validator passed "${meta.what}". That is a gap worth reporting.`)
    : (validatorCaught ? `The validator flagged "${meta.what}" too.` : `The validator passes "${meta.what}" (it is valid X3D). The browser says ${m.status}${browserCaught ? ': only the browser caught it.' : '.'}`);
  toast(line, true);
}

/* ---------------------------------------------------------------- receipt, history, arena */

function browserName() {
  const ua = navigator.userAgent;
  const m = ua.match(/(Edg|Chrome|Firefox|Version)\/(\d+)/);
  const name = m ? (m[1] === 'Version' ? 'Safari' : m[1] === 'Edg' ? 'Edge' : m[1]) : 'browser';
  return `${name} ${m ? m[2] : ''}`.trim();
}

function receiptRows() {
  const f = state.final, s = f?.summary || {}, r = state.run;
  const rows = [];
  if (r) rows.push(['run', `${r.run_id} · ${r.mode}`]);
  if (r?.parent) rows.push(['continues', `run ${r.parent}`]);
  rows.push(['verdict', f ? `${f.verdict}${f.visible ? ' · ' + visibleWords(f.visible)[1].toLowerCase() : ''}` : '—']);
  if (s.sha256) rows.push(['scene sha256', s.sha256]);
  rows.push(['validator', `x3d_mcp fork ${state.me?.validator}`]);
  rows.push(['renderer', `X_ITE ${state.me?.xite} in ${browserName()}`]);
  if (r?.writer) rows.push(['writer', `${r.writer.label} (${r.writer.blurb})`]);
  if (r?.judge) rows.push(['judge', `${r.judge.label} (${r.judge.blurb})`]);
  if (s.steps) rows.push(['steps', `${s.steps.length} · ${s.model_repairs ?? 0} by the model · ${s.validator_repairs ?? 0} by the validator`]);
  if (s.tokens_in != null) rows.push(['tokens', `${s.tokens_in.toLocaleString()} in · ${s.tokens_out.toLocaleString()} out`]);
  if (s.usd != null) rows.push(['est. cost', fmtUsd(s.usd)]);
  if (s.ms) rows.push(['time', fmtS(s.ms)]);
  if (s.stopped) rows.push(['stopped', s.stopped]);
  return rows;
}
function drawReceipt() { $('#receipt').innerHTML = receiptRows().map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join(''); }
function receiptText() { return receiptRows().map(([k, v]) => `${k}: ${v}`).join('\n'); }
function reportBundle() {
  return { receipt: Object.fromEntries(receiptRows()), steps: state.stepOrder.map((n) => { const s = state.steps.get(n); return { n, kind: s.kind, by: s.by, report: s.report, changes: s.changes, x3d: s.x3d }; }), judge: state.judge, visible: state.final?.visible };
}
function download(name, text, type) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function loadHistory() {
  const { runs } = await api('/api/history');
  if (!runs.length) { $('#history').innerHTML = '<div class="empty">No runs yet.</div>'; return; }
  $('#history').innerHTML = `<table class="f hist"><tr><th>when</th><th>writer</th><th>first → final</th><th>fixes</th><th>seen</th></tr>${runs.map((r) => `
    <tr data-id="${esc(r.run_id)}" style="cursor:pointer" title="${esc(r.prompt || '')}">
      <td class="mono">${new Date(r.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</td>
      <td>${esc(writerLabel(r.writer))}</td>
      <td><span class="pill ${r.first_verdict}">${esc(r.first_verdict || '?')}</span> → <span class="pill ${r.final_verdict}">${esc(r.final_verdict)}</span></td>
      <td class="mono small">${r.model_repairs || 0}m · ${r.validator_repairs || 0}v</td>
      <td class="small">${r.visible ? esc(r.visible.status || (r.visible.load_error ? 'error' : '')) : '—'}</td></tr>`).join('')}</table>`;
  $$('#history tr[data-id]').forEach((tr) => tr.onclick = async () => {
    const r = await api(`/api/runs/${tr.dataset.id}`);
    state.scene = r.x3d;
    $('#paste').value = r.x3d;
    showInViewer(r.x3d, { step: 'history', label: `run ${r.run_id}` });
    toast('Loaded into the viewer and the Paste tab.', true);
  });
}

async function loadArena() {
  const a = await api('/api/arena');
  if (!a || !a.reviewers) return;
  $('#arena-bars').innerHTML = a.reviewers.map((r) => `
    <div class="barrow"><span>${esc(r.name)}</span>
      <div><div class="track" title="verdicts right"><span style="width:${(100 * r.right).toFixed(0)}%;background:var(--model)"></span></div>
      <div class="track" style="height:6px;margin-top:2px" title="invalid scenes called valid"><span style="width:${(100 * r.missed).toFixed(0)}%;background:var(--fail)"></span></div></div>
      <span class="mono small">${(100 * r.right).toFixed(0)}% <span class="miss">${(100 * r.missed).toFixed(0)}%</span></span></div>`).join('');
  $('#arena-b').innerHTML = (a.with_report || []).map((r) => `
    <div class="barrow"><span>${esc(r.name)}</span><div class="track"><span style="width:${(100 * r.agreed).toFixed(0)}%;background:var(--pass)"></span></div><span class="mono small">${esc(r.agreed_n)}</span></div>`).join('');
  $('#arena-src').textContent = a.source;
}

/* ---------------------------------------------------------------- go */

setupControls();
loadMe().then(() => { loadHistory(); loadArena(); }).catch((e) => toast(e.message));
