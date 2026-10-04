/* Signal roll-up client.
 *
 * Audio rules, unchanged from what iOS Safari demands:
 *   1. AudioContext is unlocked synchronously inside the tap handler.
 *   2. Clips are fetched to Blobs and played from blob URLs. The voice gateway
 *      streams without Content-Length and iOS cannot seek a stream with no
 *      byte→time map, so the browser only ever sees a complete file.
 *   3. One <audio playsinline>. Pause is audio.pause(). That is the whole trick.
 *
 * The waveform is the interface: one block per chat, one bar per ~6 seconds, and
 * every height is a real peak measured from the decoded MP3 — so its shape is the
 * audio, not decoration. If WebAudio is unavailable it falls back to a stable
 * seeded shape per chat so the layout never jumps between loads.
 */
(function () {
  'use strict';
  const $ = s => document.querySelector(s);
  const SECONDS_PER_BAR = 6;

  const el = {
    daySelect: $('#daySelect'), dateLine: $('#dateLine'), tally: $('#tallyLine'),
    wave: $('#wave'), playBtn: $('#playBtn'), audio: $('#audio'), now: $('#nowLine'),
    scrub: $('#scrub'), scrubFill: $('#scrubFill'), elapsed: $('#elapsed'),
    rates: $('#rates'), prevBtn: $('#prevBtn'), nextBtn: $('#nextBtn'),
    notice: $('#notice'), blockNeeds: $('#blockNeeds'), blockNoted: $('#blockNoted'),
    needs: $('#needs'), noted: $('#noted'), countNeeds: $('#countNeeds'), countNoted: $('#countNoted'),
    empty: $('#empty'), colophon: $('#colophon'), schedule: $('#scheduleLine'),
    runBtn: $('#runBtn'), toast: $('#toast'),
  };

  let state = null;
  let queue = [];        // [{key,chat_id,title,url,seconds,mentioned,kind,needs_reply}]
  let index = -1;
  let rate = 1;
  let unlockCtx = null;
  let peaks = new Map();
  let pollTimer = null;
  let toastTimer = null;
  const blobs = new Map();

  const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function mmss(s) {
    s = Math.max(0, Math.round(s || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }
  function spokenLen(s) {
    s = Math.max(0, Math.round(s || 0));
    if (s < 60) return `${s} seconds`;
    const m = Math.floor(s / 60);
    return `${m} minute${m === 1 ? '' : 's'}`;
  }
  function at(ms) {
    if (!ms) return '';
    return new Date(Number(ms)).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }).toLowerCase();
  }
  function andList(items) {
    items = (items || []).filter(Boolean);
    if (items.length <= 1) return items[0] || '';
    if (items.length === 2) return `${items[0]} and ${items[1]}`;
    return `${items.slice(0, -1).join(', ')}, and ${items[items.length - 1]}`;
  }
  function toast(msg, ms) {
    el.toast.textContent = msg;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, ms || 2400);
  }

  /* ---------- iOS unlock: synchronous, inside the gesture ---------- */
  function unlock() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!unlockCtx && AC) unlockCtx = new AC();
      if (unlockCtx) {
        if (unlockCtx.state === 'suspended') unlockCtx.resume();
        const buf = unlockCtx.createBuffer(1, 1, 22050);
        const src = unlockCtx.createBufferSource();
        src.buffer = buf; src.connect(unlockCtx.destination); src.start(0);
      }
    } catch (e) { /* older browsers just play */ }
    try { const a = el.audio; if (a.src && a.paused) a.play().catch(() => {}); a.pause(); } catch (e) {}
  }

  /* ---------- waveform ---------- */
  function barsFor(t) { return clamp(Math.round((t.seconds || 24) / SECONDS_PER_BAR), 5, 20); }

  function seeded(t, n) {
    let h = 2166136261;
    for (let i = 0; i < t.key.length; i++) { h ^= t.key.charCodeAt(i); h = Math.imul(h, 16777619); }
    const out = [];
    for (let i = 0; i < n; i++) {
      h ^= h << 13; h ^= h >>> 17; h ^= h << 5; h >>>= 0;
      const cadence = (i % 4 === 3) ? 0.42 : 1;            // gaps between phrases
      const edge = 0.62 + 0.38 * Math.sin((i / n) * Math.PI);
      out.push(clamp((0.3 + (h % 1000) / 1000 * 0.7) * cadence * edge, 0.14, 1));
    }
    return out;
  }

  async function measure(t, blob) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC || !blob.arrayBuffer) return;
    try {
      const decoded = await (new AC()).decodeAudioData(await blob.arrayBuffer());
      const data = decoded.getChannelData(0);
      const n = barsFor(t);
      const block = Math.max(1, Math.floor(data.length / n));
      const out = [];
      for (let i = 0; i < n; i++) {
        let peak = 0;
        const start = i * block;
        const end = Math.min(start + block, data.length);
        for (let j = start; j < end; j += 24) {
          const v = Math.abs(data[j]);
          if (v > peak) peak = v;
        }
        out.push(clamp(peak * 1.7, 0.12, 1));
      }
      peaks.set(t.key, out);
      paintWave();
    } catch (e) { /* keep the seeded shape */ }
  }

  async function blobFor(t, attempt) {
    if (blobs.has(t.key)) return blobs.get(t.key);
    const res = await fetch(t.url, { cache: attempt ? 'reload' : 'default' });
    if (!res.ok) throw new Error(`audio ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    blobs.set(t.key, url);
    measure(t, blob);
    return url;
  }

  function heights(t) {
    if (!peaks.has(t.key)) peaks.set(t.key, seeded(t, barsFor(t)));
    return peaks.get(t.key);
  }

  /* ---------- transport ---------- */
  async function playIndex(i, fromTap) {
    if (i < 0 || i >= queue.length) return finish();
    if (fromTap) unlock();
    index = i;
    const t = queue[i];
    const a = el.audio;
    if (a.dataset.key !== t.key) {
      try {
        a.src = await blobFor(t);
      } catch (e) {
        // Second tap forces a network reload, so a cached error can never stick.
        try { a.src = await blobFor(t, 1); }
        catch (e2) { toast('af_heart\'s clip is not reachable. Tap again to refetch.'); return; }
      }
      a.dataset.key = t.key;
    }
    a.playbackRate = rate;
    paintTransport(); paintWave();
    try { await a.play(); }
    catch (e) { toast('Tap play again to start audio.'); }
    const nxt = queue[i + 1];
    if (nxt) blobFor(nxt).catch(() => {});
  }

  function toggle() {
    const a = el.audio;
    if (!a.paused && a.src) { a.pause(); return; }
    if (index < 0 || !a.src) { playIndex(0, true); return; }
    unlock();
    a.playbackRate = rate;
    a.play().catch(() => toast('Tap play again to start audio.'));
  }

  function finish() {
    try { el.audio.pause(); } catch (e) {}
    el.audio.removeAttribute('src');
    delete el.audio.dataset.key;
    index = -1;
    document.body.classList.remove('playing');
    paintWave(); paintTransport();
    el.now.innerHTML = `That is the night. <em>${queue.length} chats, ${spokenLen(state && state.totals.audio_seconds)}</em>`;
  }

  function seekFrom(clientX) {
    const a = el.audio;
    if (!a.duration || !isFinite(a.duration)) return;
    const r = el.scrub.getBoundingClientRect();
    a.currentTime = clamp((clientX - r.left) / r.width, 0, 1) * a.duration;
  }

  /* ---------- painting ---------- */
  function paintWave() {
    const a = el.audio;
    if (!queue.length) { el.wave.innerHTML = ''; return; }
    const t = index >= 0 ? queue[index] : null;
    const played = t && isFinite(a.duration) && a.duration ? clamp(a.currentTime / a.duration, 0, 1) : 0;
    el.wave.innerHTML = queue.map((q, i) => {
      const h = heights(q);
      const nowBar = i === index ? Math.min(h.length - 1, Math.floor(played * h.length)) : -1;
      const cls = ['wave-group', i < index ? 'heard' : '', i === index ? 'active' : '', q.mentioned ? 'mentions' : ''].filter(Boolean).join(' ');
      return `<button class="${cls}" data-i="${i}" style="flex:${Math.max(1, Math.round(q.seconds || 1))}"
        aria-label="${esc(q.title)}, ${mmss(q.seconds)}">${h.map((v, j) =>
          `<span class="bar${j <= nowBar && i === index ? ' heard' : ''}${j === nowBar ? ' now' : ''}" style="height:${(v * 100).toFixed(1)}%"></span>`).join('')}</button>`;
    }).join('');
  }

  function paintTransport() {
    const a = el.audio;
    const t = index >= 0 ? queue[index] : null;
    const playing = !a.paused && !!a.src;
    document.body.classList.toggle('playing', playing);

    if (t) {
      const left = queue.slice(index + 1).reduce((n, x) => n + (x.seconds || 0), 0);
      el.now.innerHTML = playing
        ? `${esc(t.title)} <em>being read out</em>`
        : `${esc(t.title)} <em>paused</em>`;
      el.elapsed.textContent = `${mmss(a.currentTime)} of ${mmss(t.seconds)}${left ? `, ${mmss(left)} still to go` : ''}`;
    } else if (queue.length) {
      el.now.innerHTML = `${queue.length} chats ready, <em>about ${spokenLen(state.totals.audio_seconds)}</em>`;
      el.elapsed.textContent = `in af_heart's voice, read at ${state.run_at || '20:00'}`;
    } else if (state.signal && state.signal.linked) {
      el.now.innerHTML = `No messages in this window <em>· send yourself a Signal note and press Run it now</em>`;
      el.elapsed.textContent = `listening live`;
    } else {
      el.now.innerHTML = `Not linked yet <em>· open /link and scan</em>`;
      el.elapsed.textContent = '';
    }
    const frac = t && isFinite(a.duration) && a.duration ? clamp(a.currentTime / a.duration, 0, 1) : 0;
    el.scrubFill.style.width = `${frac * 100}%`;
    el.scrub.setAttribute('aria-valuenow', String(Math.round(frac * 100)));
    el.playBtn.disabled = queue.length === 0;
    el.playBtn.setAttribute('aria-label', playing ? 'Pause the roll-up' : 'Play the roll-up');
    el.prevBtn.disabled = index <= 0;
    el.nextBtn.disabled = index >= queue.length - 1 && !!a.src;
  }

  function paintHeader() {
    const t = state.totals || {};
    el.dateLine.textContent = state.human_date || state.day || '';
    let line = `<b>${t.messages || 0}</b> messages in <b>${t.chats || 0}</b> chats.`;
    if (t.mentioned) line += ` <span class="hot">They said your name in ${t.mentioned}.</span>`;
    else if (t.chats) line += ' Nobody said your name.';
    if (state.building || (state.run && state.run.status === 'running')) line += ' Rolling up now.';
    else if (state.run && state.run.status === 'ok') line += ` Done at ${at(state.run.finished_at)}.`;
    else if (state.run && state.run.status === 'failed') line += ' Last roll-up failed. Details below.';
    el.tally.innerHTML = line;

    const sig = state.signal || {};
    if (!sig.linked) {
      el.notice.hidden = false;
      el.notice.className = 'notice';
      el.notice.innerHTML = 'Not connected to Signal. <a href="/link">Scan once to link this device.</a> Your iPhone stays primary and you can unlink it any time.';
      // Four distinct empty-ish states. Collapsing them into one line is how
    // "nothing came in today" got shown on a night that had summaries in it
    // but no audio: the queue only counts clips, so an af_heart failure
    // looked identical to an empty day.
    const hasChats = (state.chats || []).length > 0;
    const anyAudio = queue.length > 0;
    el.notice.hidden = false;
    if (!hasChats) {
      el.notice.className = 'notice calm';
      el.notice.textContent = `Nothing came in today. The next roll-up runs at ${state.run_at || '20:00'}.`;
    } else if (!anyAudio) {
      el.notice.className = 'notice';
      el.notice.innerHTML = `Summaries are ready, but af_heart did not answer. ` +
        `<button class="quiet" id="voiceBtn">Create the audio</button>`;
      const vb = document.getElementById('voiceBtn');
      if (vb) vb.addEventListener('click', () => el.runBtn.click());
    } else {
      el.notice.hidden = true;
    }
  }

  function metaFor(c) {
    const bits = [];
    if ((c.unread || 0) > 0) bits.push(`<span class="unread">${c.unread} new</span>`);
    if (c.self_count) bits.push(`${c.self_count} yours`);
    const last = at(c.last_ts);
    if (last) bits.push(`last at ${last}`);
    if (!bits.length) bits.push(`${c.msg_count} messages`);
    return bits.join(', ');
  }

  function entry(c, needs) {
    const key = `${state.day}:${c.chat_id}`;
    const people = (c.participants || []).slice(0, 8)
      .map(p => `<span>${esc(p.name)} <b>${p.count}</b></span>`).join('');
    const kind = c.kind === 'group' ? 'group' : c.kind === 'self' ? 'note to self' : '';
    const flag = c.mentioned
      ? `<p class="flag">${esc(andList(c.mention_labels) || 'your name came up')}</p>` : '';
    const draft = c.draft
      ? `<div class="draft">
           <p class="draft-label">Draft, yours to send</p>
           <p class="draft-text">${esc(c.draft)}</p>
           <div class="draft-foot">
             <button class="act" data-copy="${esc(key)}">Copy reply</button>
             <span class="copied" data-flag="${esc(key)}">Copied</span>
             <span class="push"></span>
             <button class="act ghost" data-read="${esc(c.chat_id)}">Mark read</button>
           </div>
         </div>`
      : `<p class="stamp">Nothing here needs you.</p>`;
    const tail = `${people ? `<p class="people">${people}</p>` : ''}
      ${c.engine === 'fallback' ? '<p class="stamp">Plain tally, the model was not answering.</p>' : ''}
      ${needs ? '' : draft}`;

    if (!needs) {
      // <button> may only contain phrasing content, so the peek text is spans
      // styled as blocks rather than a real <p>.
      return `<article class="entry compact-entry ${c.unread ? 'unread' : ''}" data-key="${esc(key)}">
        <div class="entry-top">
          <h3 class="name">${esc(c.title)}</h3>
          ${kind ? `<span class="kind">${kind}</span>` : ''}
          ${c.audio_url ? mini(c, key) : ''}
        </div>
        <button class="compact" data-toggle="${esc(key)}" aria-expanded="false">
          <span class="peek"><span class="meta">${metaFor(c)}</span><span class="prose">${esc(c.summary)}</span></span>
          <span class="caret" aria-hidden="true">›</span>
        </button>
        <div class="expando">${flag}${draft}${people ? `<p class="people">${people}</p>` : ''}</div>
      </article>`;
    }
    return `<article class="entry need ${c.unread ? 'unread' : ''}" data-key="${esc(key)}">
      <div class="entry-top">
        <h3 class="name">${esc(c.title)}</h3>
        ${kind ? `<span class="kind">${kind}</span>` : ''}
        ${c.audio_url ? mini(c, key) : ''}
      </div>
      <p class="meta">${metaFor(c)}</p>
      ${flag}
      <p class="prose">${esc(c.summary)}</p>
      ${draft}
      ${tail}
    </article>`;
  }

  function mini(c, key) {
    return `<button class="mini" data-play="${esc(key)}" aria-label="Play ${esc(c.title)}">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.5 5.2v13.6a1 1 0 0 0 1.53.85l10.6-6.8a1 1 0 0 0 0-1.7L10.03 4.35A1 1 0 0 0 8.5 5.2z"/></svg></button>`;
  }

  function paintThreads() {
    const needs = state.chats.filter(c => c.mentioned || c.needs_reply);
    const noted = state.chats.filter(c => !(c.mentioned || c.needs_reply));
    el.blockNeeds.hidden = needs.length === 0;
    el.blockNoted.hidden = noted.length === 0;
    el.empty.hidden = state.chats.length !== 0;
    el.countNeeds.textContent = needs.length ? `${needs.length}` : '';
    el.countNoted.textContent = noted.length ? `${noted.length}` : '';
    el.needs.innerHTML = needs.map(c => entry(c, true)).join('');
    el.noted.innerHTML = noted.map(c => entry(c, false)).join('');
  }

  function paintDays() {
    const days = (state.days && state.days.length) ? state.days : [state.day];
    el.daySelect.innerHTML = days.map(d =>
      `<option value="${d}"${d === state.day ? ' selected' : ''}>${d === state.day ? 'Tonight' : d}</option>`).join('');
  }

  function paintColophon() {
    const p = state.privacy || {}, sig = state.signal || {}, v = state.voice || {};
    el.schedule.innerHTML = `Runs nightly at <b>${state.run_at || '20:00'}</b> ${esc(state.tz || '')}`;
    el.colophon.innerHTML = `
      <b>Window.</b> Every chat with a message in the last ${state.window_hours || 24} hours, each with its own summary.<br><br>
      <b>Mentions.</b> Your full names, Sam T, the listed misspellings, slammy, real @mentions of your account, and replies that quote you. A bare Sam never flags, so the other Sams stay out of this list.<br><br>
      <b>Audio.</b> Each bar above is a peak measured from the real ${esc(v.voice || 'af_heart')} clip for that chat. Rendered once at ${state.run_at || '20:00'} and kept on disk, so tonight still plays if the voice box is asleep.<br><br>
      <b>Model.</b> <code>${esc(state.model || '')}</code> on omlx. Nothing leaves the tailnet.<br><br>
      <b>Privacy.</b> Raw message text is buffered only long enough to summarize, then wiped and VACUUMed. <code>${p.buffered_messages || 0}</code> rows are buffered right now. Attachments are never downloaded, so a roll-up says three photos and stops there.<br><br>
      <b>Device.</b> signal-cli <code>${esc(sig.daemon_version || '?')}</code> runs as a linked device named <code>signal-summarizer</code>. Unlinking it in iPhone settings cuts access immediately.`;
  }

  /* ---------- events ---------- */
  const a = el.audio;
  a.addEventListener('play', () => { paintTransport(); });
  a.addEventListener('pause', () => { paintTransport(); });
  a.addEventListener('timeupdate', () => { if (index >= 0) { paintTransport(); paintWave(); } });
  a.addEventListener('loadedmetadata', paintTransport);
  a.addEventListener('ended', () => {
    const done = queue[index];
    if (done) {
      fetch(`/api/read/${encodeURIComponent(done.chat_id)}`, { method: 'POST' }).catch(() => {});
      const row = document.querySelector(`[data-key="${CSS.escape(done.key)}"]`);
      if (row) {
        row.classList.remove('unread');
        const m = row.querySelector('.mini'); if (m) m.classList.add('done');
        const meta = row.querySelector('.meta .unread'); if (meta) meta.remove();
      }
    }
    playIndex(index + 1, false).catch(() => {});
  });
  a.addEventListener('error', () => {
    if (!a.src) return;
    if (queue[index]) blobs.delete(queue[index].key);
    paintTransport();
    toast('That clip would not load. Tap play to try again.');
  });

  el.playBtn.addEventListener('click', toggle);
  el.prevBtn.addEventListener('click', () => playIndex(Math.max(0, index - 1), true));
  el.nextBtn.addEventListener('click', () => playIndex(index + 1, true));
  el.scrub.addEventListener('click', e => seekFrom(e.clientX));
  el.scrub.addEventListener('touchstart', e => { if (e.touches[0]) seekFrom(e.touches[0].clientX); }, { passive: true });
  el.scrub.addEventListener('keydown', e => {
    if (!a.duration || !isFinite(a.duration)) return;
    if (e.key === 'ArrowRight') { a.currentTime = clamp(a.currentTime + 5, 0, a.duration); e.preventDefault(); }
    if (e.key === 'ArrowLeft') { a.currentTime = clamp(a.currentTime - 5, 0, a.duration); e.preventDefault(); }
  });

  el.wave.addEventListener('click', e => {
    const g = e.target.closest('.wave-group'); if (!g) return;
    const i = Number(g.dataset.i);
    if (Number.isNaN(i)) return;
    if (i === index && a.src) { seekFrom(e.clientX); return; }
    playIndex(i, true);
  });

  el.rates.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    rate = parseFloat(b.dataset.rate) || 1;
    el.rates.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    el.audio.playbackRate = rate;
    try { localStorage.setItem('ss-rate', String(rate)); } catch (err) {}
  });

  el.daySelect.addEventListener('change', () => {
    try { el.audio.pause(); el.audio.removeAttribute('src'); } catch (e) {}
    delete el.audio.dataset.key;
    index = -1;
    load(el.daySelect.value);
  });

  document.addEventListener('click', async e => {
    const toggleBtn = e.target.closest('[data-toggle]');
    if (toggleBtn) {
      const row = toggleBtn.closest('.entry');
      const open = row.classList.toggle('open');
      toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      return;
    }
    const play = e.target.closest('[data-play]');
    if (play) {
      const i = queue.findIndex(q => q.key === play.dataset.play);
      if (i < 0) return;
      if (!el.audio.paused && index === i) { el.audio.pause(); return; }
      await playIndex(i, true);
      return;
    }
    const copy = e.target.closest('[data-copy]');
    if (copy) {
      const row = copy.closest('.entry');
      const text = row && row.querySelector('.draft-text');
      const value = text ? text.textContent.trim() : '';
      let ok = false;
      try { await navigator.clipboard.writeText(value); ok = true; }
      catch (err) {
        const ta = document.createElement('textarea');
        ta.value = value; ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
        document.body.appendChild(ta); ta.select();
        try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
        ta.remove();
      }
      const flag = row && row.querySelector('[data-flag]');
      if (flag) { flag.classList.add('show'); setTimeout(() => flag.classList.remove('show'), 1800); }
      if (!ok) toast('Select the text and copy it yourself.');
      return;
    }
    const read = e.target.closest('[data-read]');
    if (read) {
      fetch(`/api/read/${encodeURIComponent(read.dataset.read)}`, { method: 'POST' }).catch(() => {});
      const row = read.closest('.entry');
      if (row) {
        row.classList.remove('unread');
        const u = row.querySelector('.meta .unread'); if (u) u.remove();
        const mini = row.querySelector('.mini'); if (mini) mini.classList.add('done');
      }
      toast('Marked read.');
    }
  });

  el.runBtn.addEventListener('click', async () => {
    el.runBtn.disabled = true;
    el.runBtn.textContent = 'Running';
    try {
      const r = await fetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const j = await r.json();
      toast(j.status === 'busy' ? 'One is already running.' : 'Reading the last 24 hours.');
      poll();
    } catch (err) {
      toast('The summarizer is not answering.');
      el.runBtn.disabled = false; el.runBtn.textContent = 'Run it now';
    }
  });

  addEventListener('keydown', e => {
    if (e.target.matches('input,select,textarea,button')) return;
    if (e.code === 'Space') { e.preventDefault(); toggle(); }
    else if (e.key === 'ArrowRight' && e.shiftKey) { e.preventDefault(); playIndex(index + 1, true); }
    else if (e.key === 'ArrowLeft' && e.shiftKey) { e.preventDefault(); playIndex(Math.max(0, index - 1), true); }
  });

  /* ---------- load ---------- */
  async function load(day) {
    const res = await fetch(day ? `/api/day/${day}` : '/api/state', { cache: 'no-store' });
    if (res.status === 401) { location.reload(); return; }
    state = await res.json();
    queue = [];
    if (state.briefing && state.briefing.audio_url) {
      queue.push({ key: `${state.day}:intro`, chat_id: 'intro', title: `Roll-up for ${state.human_date || state.day}`,
                   url: state.briefing.audio_url, seconds: state.briefing.audio_seconds || 0, mentioned: 0, kind: 'intro' });
    }
    for (const c of state.chats) {
      if (c.audio_url) queue.push({ key: `${state.day}:${c.chat_id}`, chat_id: c.chat_id, title: c.title,
        url: c.audio_url, seconds: c.audio_seconds || 0, mentioned: c.mentioned || 0,
        needs_reply: c.needs_reply, kind: c.kind });
    }
    paintHeader(); paintDays(); paintWave(); paintTransport(); paintThreads(); paintColophon();
    if (state.building) poll();
  }

  function poll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      try {
        const r = await fetch('/api/build-status', { cache: 'no-store' });
        if (!r.ok) throw new Error(`build-status ${r.status}`);
        const j = await r.json();
        if (j.running || j.status === 'busy') { poll(); return; }
        el.runBtn.disabled = false; el.runBtn.textContent = 'Run it now';
        if (j.result && j.result.status === 'failed') toast('Roll-up failed. The details are in the panel.');
        else { toast('Roll-up ready.'); blobs.forEach(u => URL.revokeObjectURL(u)); blobs.clear(); peaks.clear(); index = -1; load(state && state.day); }
      } catch (err) { poll(); }
    }, 5000);
  }

  try {
    const saved = parseFloat(localStorage.getItem('ss-rate') || '1');
    if ([0.9, 1, 1.3].includes(saved)) {
      rate = saved;
      el.rates.querySelectorAll('button').forEach(x => x.classList.toggle('on', parseFloat(x.dataset.rate) === rate));
    }
  } catch (e) {}

  addEventListener('pagehide', () => { try { el.audio.pause(); } catch (e) {} });

  load().catch(() => { el.tally.textContent = 'The summarizer is not answering.'; });
})();
