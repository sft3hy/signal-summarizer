/* /link — the pairing handshake.
 *
 * signal-cli's startLink URI is honoured by Signal's servers for roughly a
 * minute, so this page mints a new one every 45 seconds and redraws the code.
 * finishLink is already running server-side in a thread: scanning is all the
 * phone has to do. The fuse bar is the only motion, and it is real — it is the
 * lifetime of the URI you are looking at.
 */
(function () {
  'use strict';
  const $ = s => document.querySelector(s);
  const REFRESH_MS = 45000, POLL_MS = 2000;

  const el = {
    qr: $('#qr'), fuse: $('#fuse'), fuseFill: $('#fuseFill'), cd: $('#cd'),
    waitMsg: $('#waitMsg'), uri: $('#uriBox'), wait: $('#waitWrap'),
    ok: $('#okWrap'), okMsg: $('#okMsg'), err: $('#errWrap'), errMsg: $('#errMsg'),
    toast: $('#toast'),
  };

  let bornAt = 0, expiresAt = 0, refreshT = null, pollT = null, tickT = null, busy = false;

  function toast(m, ms) {
    el.toast.textContent = m; el.toast.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => { el.toast.hidden = true; }, ms || 2600);
  }

  function draw(text) {
    el.qr.innerHTML = '';
    try {
      new QRCode(el.qr, {
        text: text, width: 228, height: 228, colorDark: '#0d1420', colorLight: '#ffffff',
        correctLevel: QRCode.CorrectLevel.M,
      });
    } catch (e) {
      el.qr.innerHTML = '<p style="width:228px;height:228px;display:grid;place-items:center;color:#444;font-size:13px;padding:14px;text-align:center">The code would not render. The URI is below.</p>';
    }
    el.uri.textContent = text;
  }

  async function start() {
    if (busy) return;
    busy = true;
    try {
      const r = await fetch('/api/link/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const j = await r.json();
      if (j.linked || j.state === 'linked') return done(j);
      if (!j.uri) {
        el.cd.textContent = j.detail || 'signal-cli did not answer';
        el.waitMsg.textContent = 'Retrying';
        if (/not (running|answering)|unreachable/i.test(String(j.detail || ''))) fail(j.detail);
        return;
      }
      el.wait.hidden = false; el.err.hidden = true;
      draw(j.uri);
      bornAt = Date.now();
      expiresAt = bornAt + 55000;
      el.fuse.classList.remove('spent');
      el.waitMsg.textContent = 'Waiting for your phone';
    } catch (e) {
      el.cd.textContent = 'Lost the summarizer, retrying';
    } finally {
      busy = false;
    }
  }

  function tick() {
    if (!expiresAt) return;
    const life = expiresAt - bornAt || 1;
    const left = Math.max(0, expiresAt - Date.now());
    el.fuseFill.style.transform = `scaleX(${(left / life).toFixed(3)})`;
    if (left <= 0) { el.fuse.classList.add('spent'); el.cd.textContent = 'expired, new code coming'; }
    else el.cd.textContent = `this code is good for ${Math.ceil(left / 1000)} more seconds`;
  }

  async function poll() {
    try {
      const r = await fetch('/api/link/status', { cache: 'no-store' });
      const j = await r.json();
      if (j.linked || j.state === 'linked') return done(j);
      if (j.state === 'error') { el.cd.textContent = j.detail || 'pairing failed'; }
      else if (j.state === 'waiting' && j.detail) { el.waitMsg.textContent = 'Still waiting for your phone'; }
      if (j.uri_expired && !busy) { clearTimeout(refreshT); start(); }
    } catch (e) { /* keep trying */ }
  }

  function done(j) {
    clearInterval(tickT); clearTimeout(pollT); clearTimeout(refreshT);
    el.wait.hidden = true; el.err.hidden = true; el.ok.hidden = false;
    const aci = String(j.aci || j.account || '');
    el.okMsg.innerHTML = aci
      ? `signal-cli is receiving now, as <code>${esc(aci.slice(0, 8))}…</code>. Reading the last 24 hours.`
      : 'signal-cli is receiving now. Reading the last 24 hours.';
  }

  function fail(detail) {
    clearInterval(tickT); clearTimeout(pollT);
    el.wait.hidden = true; el.ok.hidden = true; el.err.hidden = false;
    if (detail) el.errMsg.textContent = String(detail).slice(0, 240);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  start();
  pollT = setInterval(poll, POLL_MS);
  refreshT = setInterval(start, REFRESH_MS);
  tickT = setInterval(tick, 200);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) start(); });
})();
