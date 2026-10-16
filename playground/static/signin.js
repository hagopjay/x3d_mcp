'use strict';
(async function () {
  const q = new URLSearchParams(location.search);
  if (q.get('denied') === 'pass') document.getElementById('denied').textContent = 'That pass link is not valid any more. Ask the owner for a new one.';
  let cfg = {};
  try { cfg = await (await fetch('/auth/config')).json(); } catch (e) { /* no Google sign-in */ }
  if (!cfg.oauth_client_id) return;
  document.getElementById('google').classList.remove('hidden');
  document.getElementById('nogoogle').textContent = 'No Google account? Open the pass link the owner sent you.';
  const s = document.createElement('script');
  s.src = 'https://accounts.google.com/gsi/client';
  s.async = true;
  s.onload = () => {
    google.accounts.id.initialize({
      client_id: cfg.oauth_client_id, ux_mode: 'popup', auto_select: false,
      callback: async (resp) => {
        const r = await fetch('/auth/google', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Playground': '1' }, body: JSON.stringify({ credential: resp.credential }) });
        const d = await r.json().catch(() => ({}));
        if (r.ok) location.href = '/';
        else document.getElementById('denied').textContent = d.error || 'Sign-in failed.';
      },
    });
    google.accounts.id.renderButton(document.getElementById('gbtn'), { theme: 'filled_black', size: 'large', shape: 'pill', text: 'signin_with' });
  };
  document.head.appendChild(s);
})();
