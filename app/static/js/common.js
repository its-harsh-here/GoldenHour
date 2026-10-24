/* Shared helpers: API client (sends the demo role), INR formatting, icons, SLA sun-dial, toasts, captions. */
window.GH = (() => {
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const state = { role: store.get('gh.role', 'PROCESSOR') };
  const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  async function api(method, path, body) {
    const res = await fetch(path, {
      method, headers: { 'Content-Type': 'application/json', 'X-Role': state.role },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const err = new Error((data && data.detail) || res.statusText);
      err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  }

  function inr(n, { sign = false } = {}) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
    const v = Math.round(Number(n));
    let s = String(Math.abs(v));
    if (s.length > 3) {
      let head = s.slice(0, -3); const tail = s.slice(-3); const parts = [];
      while (head.length > 2) { parts.unshift(head.slice(-2)); head = head.slice(0, -2); }
      if (head) parts.unshift(head);
      s = parts.join(',') + ',' + tail;
    }
    return (v < 0 ? '−' : sign && v > 0 ? '+' : '') + '₹' + s;
  }

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const icon = (id, cls = '') => `<svg class="icon ${cls}" aria-hidden="true"><use href="/static/vendor/icons.svg#${id}"/></svg>`;

  function ago(ts) {
    const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
    if (m < 1) return 'just now';
    if (m < 60) return `${m} min ago`;
    const h = Math.floor(m / 60);
    return `${h} h ${m % 60} min ago`;
  }
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  /* ---- SLA sun-dial: fills pale gold -> deep amber across the window; coral when breached ---- */
  const lerp = (a, b, t) => Math.round(a + (b - a) * t);
  function dialColor(f) {
    const a = [255, 227, 163], b = [217, 119, 6];
    const t = Math.min(Math.max(f, 0), 1);
    return `rgb(${lerp(a[0], b[0], t)},${lerp(a[1], b[1], t)},${lerp(a[2], b[2], t)})`;
  }
  function dial(sla, size = 'sm') {
    const px = size === 'lg' ? 124 : 50;
    const stroke = size === 'lg' ? 9 : 4.5;
    const r = (px - stroke) / 2 - 1;
    return `<div class="dial ${size}" data-live="1" data-elapsed="${sla.elapsed_s}" data-budget="${sla.budget_s}"
      data-paused="${sla.paused ? 1 : 0}" data-done="${sla.done ? 1 : 0}" data-t="${Date.now()}" data-r="${r}" data-px="${px}" data-stroke="${stroke}">
      ${dialInner(sla.elapsed_s, sla.budget_s, sla.paused, sla.done, r, px, stroke)}</div>`;
  }
  function dialInner(elapsed, budget, paused, done, r, px, stroke) {
    const f = Math.min(elapsed / budget, 1);
    const breached = elapsed > budget;
    const c = 2 * Math.PI * r;
    const color = done ? (breached ? 'var(--coral)' : 'var(--teal)') : breached ? 'var(--coral)' : paused ? 'var(--violet)' : dialColor(f);
    const minsLeft = Math.ceil((budget - elapsed) / 60);
    const big = done ? Math.max(1, Math.round(elapsed / 60)) : breached ? `+${Math.ceil((elapsed - budget) / 60)}` : Math.max(0, minsLeft);
    const unit = done ? 'min taken' : breached ? 'min over' : paused ? 'paused' : 'min left';
    const cls = done ? 'done' : breached ? 'breached' : paused ? 'paused' : '';
    return `<svg width="${px}" height="${px}" viewBox="0 0 ${px} ${px}" aria-hidden="true">
        <circle class="track" cx="${px / 2}" cy="${px / 2}" r="${r}" fill="none" stroke-width="${stroke}"/>
        <circle class="arc" cx="${px / 2}" cy="${px / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="${stroke}"
          stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - Math.max(f, 0.004))}" ${paused && !done ? 'stroke-opacity=".75"' : ''}/>
      </svg><div class="center"><div class="mins ${cls}">${big}</div><div class="unit">${unit}</div></div>
      <span class="sr-only">${unit} ${big}</span>`;
  }
  function tickDials() {
    document.querySelectorAll('.dial[data-live]').forEach((el) => {
      const d = el.dataset;
      const live = d.paused !== '1' && d.done !== '1';
      const elapsed = Number(d.elapsed) + (live ? (Date.now() - Number(d.t)) / 1000 : 0);
      el.classList.toggle('breached', elapsed > Number(d.budget));
      el.innerHTML = dialInner(elapsed, Number(d.budget), d.paused === '1', d.done === '1', Number(d.r), Number(d.px), Number(d.stroke));
    });
  }

  function toast(msg, kind = 'ok', ms = 4500) {
    const box = document.getElementById('toasts');
    if (!box) return;
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = `${icon(kind === 'err' ? 'i-alert' : 'i-check')}<div>${msg}</div>`;
    box.appendChild(el);
    setTimeout(() => el.remove(), ms);
  }

  function caption(text, step = '') {
    const slot = document.getElementById('caption-slot');
    if (!slot) return;
    slot.innerHTML = text ? `<div class="caption">${step ? `<span class="step">${esc(step)}</span>` : ''}<span>${text}</span></div>` : '';
  }

  function countUp(el, to, { decimals = 0, suffix = '', format = null } = {}) {
    if (to === null || to === undefined) { el.textContent = '—'; return; }
    const render = (v) => { el.innerHTML = (format ? format(v) : v.toFixed(decimals)) + suffix; };
    if (reduced) { render(to); return; }
    const start = performance.now(), dur = 900;
    const step = (now) => {
      const t = Math.min((now - start) / dur, 1);
      render(to * (1 - Math.pow(1 - t, 3)));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  return { state, store, api, inr, esc, icon, ago, clock, dial, tickDials, toast, caption, countUp, sleep, reduced };
})();
