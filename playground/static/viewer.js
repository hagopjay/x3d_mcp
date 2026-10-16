'use strict';
/* Runs inside an iframe sandboxed without allow-same-origin: scenes (and any
   Script node in them) get an opaque origin, no cookies, no API.
   Protocol: parent -> {type:'load', id, x3d}; viewer -> {type:'result', id, ...}.
   The frame check mirrors analyze_frame in src/tools/render.py: the background
   is the most common colour of a coarse 64x36 sample; a pixel counts when the
   luma of its difference from the background exceeds 12; under 0.05% of
   pixels is "blank", under 1% is "sparse". A verdict waits for the scene to
   settle (see settle below). */
(function () {
  const BLANK = 0.0005, SPARSE = 0.01, TOL = 12;
  const canvasEl = document.getElementById('c');
  const browser = X3D.getBrowser(canvasEl);
  const messages = [];
  const keep = (kind, args) => {
    const text = args.map((a) => (a && a.message) ? a.message : String(a)).join(' ');
    if (text) messages.push(`${kind}: ${text.slice(0, 300)}`);
    if (messages.length > 40) messages.shift();
  };
  for (const k of ['warn', 'error']) {
    const orig = console[k].bind(console);
    console[k] = (...a) => { keep(k, a); orig(...a); };
  }
  window.addEventListener('error', (e) => keep('error', [e.message]));
  window.addEventListener('unhandledrejection', (e) => keep('error', [e.reason]));

  const frames = (n) => new Promise((res) => { const step = () => (n-- <= 0 ? res() : requestAnimationFrame(step)); requestAnimationFrame(step); });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function glCanvas() {
    try { const c = browser.getCanvas(); if (c && c.tagName) return c; if (c && c[0]) return c[0]; } catch (e) { /* fall through */ }
    return canvasEl.shadowRoot ? canvasEl.shadowRoot.querySelector('canvas') : canvasEl.querySelector('canvas');
  }

  function analyze(withThumb) {
    const src = glCanvas();
    if (!src || !src.width) return { status: 'unknown', coverage: 0, width: 0, height: 0 };
    const w = src.width, h = src.height;
    const c2 = document.createElement('canvas'); c2.width = w; c2.height = h;
    const ctx = c2.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(src, 0, 0);
    const px = ctx.getImageData(0, 0, w, h).data;
    const counts = new Map();
    for (let y = 0; y < 36; y++) for (let x = 0; x < 64; x++) {
      const i = ((Math.floor((y + 0.5) * h / 36) * w) + Math.floor((x + 0.5) * w / 64)) * 4;
      const key = (px[i] << 16) | (px[i + 1] << 8) | px[i + 2];
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    let bg = 0, best = -1;
    for (const [k, v] of counts) if (v > best) { best = v; bg = k; }
    const br = (bg >> 16) & 255, bgc = (bg >> 8) & 255, bb = bg & 255;
    let n = 0, x0 = w, y0 = h, x1 = -1, y1 = -1;
    for (let y = 0, i = 0; y < h; y++) for (let x = 0; x < w; x++, i += 4) {
      const d = (299 * Math.abs(px[i] - br) + 587 * Math.abs(px[i + 1] - bgc) + 114 * Math.abs(px[i + 2] - bb)) / 1000;
      if (d > TOL) { n++; if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    }
    const f = n / (w * h);
    const out = {
      status: f < BLANK ? 'blank' : f < SPARSE ? 'sparse' : 'ok', coverage: +f.toFixed(6),
      background: [br, bgc, bb], bbox: x1 >= 0 ? [x0, y0, x1 + 1, y1 + 1] : null, width: w, height: h,
    };
    if (withThumb) {
      const t = document.createElement('canvas'); t.width = 320; t.height = Math.round(320 * h / w);
      t.getContext('2d').drawImage(src, 0, 0, t.width, t.height);
      out.thumb = t.toDataURL('image/jpeg', 0.72);
    }
    return out;
  }

  const loadingNow = () => { try { return !!(browser.getBrowserLoading && browser.getBrowserLoading()); } catch (e) { return false; } };

  /* A scene is judged once it has settled: X_ITE has finished loading (components,
     textures, Inlines), then two samples in a row agree. Shaders can still compile
     after loading ends (physically based materials on a software GPU take seconds),
     so a blank frame is only reported after MAX_MS of sampling, never on the
     first look. */
  const MAX_MS = 8000;
  async function settle(t0) {
    const end = t0 + MAX_MS;
    while (loadingNow() && performance.now() < end) await sleep(100);
    await frames(4);
    let prev = null, r = null, samples = 0;
    for (;;) {
      r = analyze(false); samples++;
      if (prev && r.status !== 'blank' && Math.abs(r.coverage - prev.coverage) < 0.002) break;
      if (performance.now() >= end) break;
      prev = r;
      await sleep(r.status === 'blank' ? 400 : 250);
      await frames(2);
    }
    return { ...analyze(true), samples };
  }

  let busy = Promise.resolve();
  window.addEventListener('message', (ev) => {
    if (ev.source !== window.parent) return;
    const m = ev.data || {};
    if (m.type !== 'load' || typeof m.x3d !== 'string') return;
    busy = busy.then(() => load(m)).catch(() => {});
  });

  async function load(m) {
    messages.length = 0;
    const t0 = performance.now();
    let loadError = null;
    try {
      const scene = await browser.createX3DFromString(m.x3d);
      await browser.replaceWorld(scene);
    } catch (e) {
      loadError = String((e && e.message) || e).slice(0, 400);
      try { await browser.replaceWorld(null); } catch (e2) { /* empty world */ }
    }
    let r;
    try { r = await settle(t0); } catch (e) { r = { status: 'unknown', coverage: 0, error: String(e) }; }
    window.parent.postMessage({ type: 'result', id: m.id, ...r, load_error: loadError,
      messages: messages.slice(-30), ms: Math.round(performance.now() - t0) }, '*');
  }

  window.parent.postMessage({ type: 'ready' }, '*');
})();
