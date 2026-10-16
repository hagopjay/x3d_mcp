'use strict';
const $ = (s) => document.querySelector(s);
const H = { 'Content-Type': 'application/json', 'X-Playground': '1' };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const when = (t) => t ? new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
const usd = (x) => `$${(x || 0).toFixed(x < 0.1 ? 4 : 2)}`;
let D = null, dirty = false;

function toast(msg, ok) {
  const t = $('#toast'); t.textContent = msg; t.classList.toggle('ok', !!ok); t.classList.remove('hidden');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add('hidden'), 4000);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts, headers: H });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}
const lines = (id) => $(id).value.split('\n').map((s) => s.trim()).filter(Boolean);
['#domains', '#emails', '#blocked', '#admins', '#oauth'].forEach((id) => $(id).addEventListener('input', () => { dirty = true; }));

const LIMIT_LABELS = {
  user_per_hour: 'runs per person per hour', user_per_day: 'runs per person per day', ip_per_hour: 'runs per network per hour',
  global_runs_per_day: 'runs per day, everyone', usd_per_day: 'model spend per day, USD', usd_phase: 'model spend this phase, USD',
  validate_per_hour: 'validations per person per hour', max_prompt_chars: 'prompt length, characters', max_scene_chars: 'scene size, characters',
  max_repairs: 'most model repairs per run', run_timeout_s: 'run time limit, seconds',
};

function fill(force) {
  const a = D.access;
  $('#b-validator').textContent = D.validator; $('#b-xite').textContent = D.xite;
  $('#b-store').textContent = `store ${D.store}${D.runlog ? ' · BigQuery log on' : ''}`;
  $('#b-updated').textContent = a.updated ? `list saved ${when(a.updated)} by ${a.updated_by}` : 'list never saved';
  $('#paused').checked = !!a.paused;
  if (force || !dirty) {
    $('#domains').value = (a.domains || []).join('\n'); $('#emails').value = (a.emails || []).join('\n');
    $('#blocked').value = (a.blocked || []).join('\n'); $('#admins').value = (a.admins || []).join('\n');
    $('#oauth').value = a.oauth_client_id || '';
    $('#limits').innerHTML = Object.entries(D.limits).map(([k, v]) => `<label class="small" for="lim-${k}">${esc(LIMIT_LABELS[k] || k)}</label><input type="number" step="any" min="0" id="lim-${k}" data-k="${k}" value="${v}">`).join('');
    $('#writers-on').innerHTML = D.writers.map((w) => `<label class="toggle small" title="${esc(w.blurb)}"><input type="checkbox" data-w="${w.code}" ${(a.writers_off || []).includes(w.code) ? '' : 'checked'} ${w.enabled || !(a.writers_off || []).includes(w.code) ? '' : ''}> ${esc(w.label)}</label>`).join('');
  }
  const c = D.counters, runs = D.runs;
  const first = runs.filter((r) => r.first_verdict === 'pass').length, fin = runs.filter((r) => r.final_verdict === 'pass').length;
  const judged = runs.filter((r) => r.judge && r.judge.compare && r.judge.compare.agree != null);
  const agree = judged.filter((r) => r.judge.compare.agree).length;
  const visible = runs.filter((r) => r.visible && r.visible.status);
  const seen = visible.filter((r) => r.visible.status === 'ok').length;
  const today = new Date().toISOString().slice(0, 10);
  const denials = D.events.filter((e) => /denied|limited/.test(e.kind) && new Date(e.ts * 1000).toISOString().slice(0, 10) === today).length;
  const stats = [
    [`${c.global_day} / ${c.global_day_limit}`, 'runs today, everyone'],
    [`${usd(c.usd_day)} / ${usd(c.usd_day_limit)}`, 'model spend today'],
    [`${usd(c.usd_phase)} / ${usd(c.usd_phase_limit)}`, 'model spend this phase'],
    [`${runs.length ? Math.round(100 * first / runs.length) : 0}% → ${runs.length ? Math.round(100 * fin / runs.length) : 0}%`, `valid at draft 1 → after the loop (${runs.length} runs)`],
    [`${judged.length ? Math.round(100 * agree / judged.length) : 0}%`, `judge alone agreed with the validator (${judged.length})`],
    [`${visible.length ? Math.round(100 * seen / visible.length) : 0}%`, `final scenes the browser saw (${visible.length})`],
    [String(D.users.filter((u) => u.last_seen && (Date.now() / 1000 - u.last_seen) < 86400).length), 'people active in 24 h'],
    [String(denials), 'refusals today'],
  ];
  $('#stats').innerHTML = stats.map(([b, s]) => `<div><b>${esc(b)}</b><span>${esc(s)}</span></div>`).join('');
  spark(runs);
  const byW = {};
  for (const r of runs) {
    const w = (byW[r.writer] = byW[r.writer] || { n: 0, first: 0, fin: 0, mrep: 0, vrep: 0, usd: 0, ms: 0 });
    w.n++; w.first += r.first_verdict === 'pass'; w.fin += r.final_verdict === 'pass'; w.mrep += r.model_repairs || 0; w.vrep += r.validator_repairs || 0; w.usd += r.usd || 0; w.ms += r.ms || 0;
  }
  const label = (code) => (D.writers.find((w) => w.code === code) || {}).label || code;
  $('#by-writer').innerHTML = '<tr><th>writer</th><th>runs</th><th>first</th><th>final</th><th>model fixes</th><th>validator fixes</th><th>avg time</th><th>est. cost</th></tr>' +
    Object.entries(byW).map(([k, w]) => `<tr><td>${esc(label(k))}</td><td>${w.n}</td><td>${Math.round(100 * w.first / w.n)}%</td><td>${Math.round(100 * w.fin / w.n)}%</td><td>${w.mrep}</td><td>${w.vrep}</td><td>${(w.ms / w.n / 1000).toFixed(0)} s</td><td>${usd(w.usd)}</td></tr>`).join('');
  $('#passes').innerHTML = '<tr><th>for</th><th>created</th><th>expires</th><th>uses</th><th>last used</th><th></th></tr>' + D.passes.map((p) => `
    <tr><td>${esc(p.label)}${p.admin ? ' <span class="pill wait">admin</span>' : ''}<div class="faint tiny">${esc(p.email || '')}</div></td><td>${when(p.created)}</td>
    <td>${p.expires ? when(p.expires) : 'never'}</td><td>${p.uses || 0}</td><td>${when(p.last_used)}</td>
    <td>${p.revoked ? '<span class="miss">revoked</span>' : `<button class="chip-btn" data-copy="${esc(p.link)}">copy link</button> <button class="chip-btn" data-revoke="${esc(p.id)}">revoke</button>`}</td></tr>`).join('');
  document.querySelectorAll('[data-copy]').forEach((b) => b.onclick = async () => { await navigator.clipboard.writeText(location.origin + b.dataset.copy); toast('Link copied. Send it privately.', true); });
  document.querySelectorAll('[data-revoke]').forEach((b) => b.onclick = async () => { if (!confirm('Revoke this pass? Its link stops working at once.')) return; await api(`/api/admin/passes/${b.dataset.revoke}/revoke`, { method: 'POST', body: '{}' }); refresh(); });
  $('#runs').innerHTML = '<tr><th></th><th>when</th><th>who</th><th>writer</th><th>mode</th><th>first → final</th><th>fixes m · v</th><th>judge</th><th>browser</th><th>cost</th><th>time</th><th>prompt</th></tr>' + runs.map((r) => `
    <tr><td>${r.thumb ? `<img class="thumb" src="${esc(r.thumb)}" alt="">` : ''}</td><td class="mono small">${when(r.ts)}</td><td class="small">${esc(r.who)}</td><td>${esc(label(r.writer))}</td><td class="small">${esc(r.mode)}</td>
    <td><span class="pill ${r.first_verdict}">${esc(r.first_verdict)}</span> → <span class="pill ${r.final_verdict}">${esc(r.final_verdict)}</span></td>
    <td class="mono small">${r.model_repairs || 0} · ${r.validator_repairs || 0}</td>
    <td class="small">${r.judge && r.judge.compare ? (r.judge.compare.agree ? '<span class="hit">agreed</span>' : '<span class="miss">disagreed</span>') + ` ${r.judge.compare.caught}/${r.judge.compare.validator_errors}` : '—'}</td>
    <td class="small">${r.visible ? esc(r.visible.load_error ? 'not loaded' : r.visible.status) : '—'}</td><td class="mono small">${usd(r.usd)}</td><td class="mono small">${((r.ms || 0) / 1000).toFixed(0)} s</td>
    <td class="small" style="max-width:340px">${esc((r.prompt || '').slice(0, 140))}</td></tr>`).join('');
  $('#users').innerHTML = '<tr><th>who</th><th>how</th><th>runs</th><th>first seen</th><th>last seen</th></tr>' + D.users.map((u) => `
    <tr><td class="small">${esc(u.display)}</td><td class="small">${esc(u.kind)}${u.rule ? ' · ' + esc(u.rule) : ''}</td><td>${u.runs || 0}</td><td class="small">${when(u.first_seen)}</td><td class="small">${when(u.last_seen)}</td></tr>`).join('');
  const ev = D.events.filter((e) => !$('#only-denied').checked || /denied|limited|too-long/.test(e.kind));
  $('#events').innerHTML = '<tr><th>when</th><th>what</th><th>who</th><th>detail</th><th>network</th></tr>' + ev.map((e) => `
    <tr><td class="mono small">${when(e.ts)}</td><td class="small ${/denied|limited/.test(e.kind) ? 'miss' : ''}">${esc(e.kind)}</td><td class="small">${esc(e.who || '')}</td>
    <td class="small">${esc(e.reason || e.method || e.writer || e.label || e.keys || e.path || '')}${e.rule ? ' · ' + esc(e.rule) : ''}</td><td class="mono tiny faint">${esc(e.ip || '')}</td></tr>`).join('');
}

function spark(runs) {
  const now = Date.now() / 1000, bins = new Array(48).fill(0);
  for (const r of runs) { const h = Math.floor((now - r.ts) / 3600); if (h >= 0 && h < 48) bins[47 - h]++; }
  const max = Math.max(1, ...bins), w = 600 / 48;
  $('#spark').innerHTML = bins.map((v, i) => `<rect x="${i * w + 1}" y="${66 - 60 * v / max}" width="${w - 2}" height="${60 * v / max}" rx="2" fill="#67a6ff" opacity="${v ? 0.9 : 0.15}"><title>${v} runs</title></rect>`).join('') +
    `<text x="0" y="10" fill="#8b93a7" font-size="10">${max} max/h</text><text x="600" y="10" fill="#8b93a7" font-size="10" text-anchor="end">now</text>`;
}

async function refresh(force) {
  try { D = await api('/api/admin/overview'); fill(force); } catch (e) { toast(e.message); }
}

$('#save-access').onclick = async () => {
  await api('/api/admin/access', { method: 'PUT', body: JSON.stringify({ domains: lines('#domains'), emails: lines('#emails'), blocked: lines('#blocked'), admins: lines('#admins'), oauth_client_id: $('#oauth').value.trim() }) });
  dirty = false; toast('Saved. New sign-ins use it at once.', true); refresh(true);
};
$('#save-limits').onclick = async () => {
  const limits = {}; document.querySelectorAll('[data-k]').forEach((i) => { if (i.value !== '') limits[i.dataset.k] = Number(i.value); });
  const writers_off = [...document.querySelectorAll('[data-w]')].filter((c) => !c.checked).map((c) => c.dataset.w);
  await api('/api/admin/access', { method: 'PUT', body: JSON.stringify({ limits, writers_off }) });
  toast('Limits saved.', true); refresh(true);
};
$('#paused').onchange = async (e) => { await api('/api/admin/access', { method: 'PUT', body: JSON.stringify({ paused: e.target.checked }) }); toast(e.target.checked ? 'Model runs paused.' : 'Model runs resumed.', true); refresh(); };
$('#only-denied').onchange = () => fill();
$('#make-pass').onclick = async () => {
  const num = (id) => $(id).value === '' ? null : Number($(id).value);
  try {
    const p = await api('/api/admin/passes', { method: 'POST', body: JSON.stringify({ label: $('#p-label').value, email: $('#p-email').value, days: Number($('#p-days').value || 0), per_hour: num('#p-hour'), per_day: num('#p-day'), admin: $('#p-admin').checked }) });
    const link = location.origin + p.link;
    $('#new-pass').innerHTML = `New pass for <b>${esc(p.label)}</b>: <span>${esc(link)}</span> <button class="chip-btn" id="copy-new">copy</button>`;
    $('#copy-new').onclick = async () => { await navigator.clipboard.writeText(link); toast('Copied. Send it privately.', true); };
    $('#p-label').value = ''; $('#p-email').value = ''; refresh();
  } catch (e) { toast(e.message); }
};
refresh(true);
setInterval(refresh, 10000);
