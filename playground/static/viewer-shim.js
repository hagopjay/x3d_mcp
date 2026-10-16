'use strict';
/* The viewer is sandboxed without allow-same-origin, so Web Storage throws.
   X_ITE keeps its user preferences there; give it an in-memory stand-in. */
(function () {
  function MemoryStorage() { const m = new Map(); return {
    get length() { return m.size; }, key: (i) => [...m.keys()][i] ?? null,
    getItem: (k) => (m.has(String(k)) ? m.get(String(k)) : null), setItem: (k, v) => { m.set(String(k), String(v)); },
    removeItem: (k) => { m.delete(String(k)); }, clear: () => m.clear() }; }
  for (const name of ['localStorage', 'sessionStorage']) {
    let ok = false;
    try { ok = !!window[name]; } catch (e) { ok = false; }
    if (!ok) { try { Object.defineProperty(window, name, { configurable: true, value: MemoryStorage() }); } catch (e) { /* leave it */ } }
  }
})();
