/* GoldenHour ops console: Inbox, Case (live trace, decision card, redaction view, approvals, outbox),
   Dashboard and Audit. Vanilla JS; the role switcher sends X-Role on every request. */
(() => {
  const { api, inr, esc, icon, dial, toast, caption, countUp, sleep } = GH;
  const $ = (sel, root = document) => root.querySelector(sel);
  const S = { meta: null, cases: [], tab: 'inbox', caseId: null, view: null, events: [], es: null, docMode: 'original', charts: {}, draft: {}, demo: false };

  const AGENTS = [
    { id: 'intake', name: 'Intake Agent', role: 'Redact PHI · screen injection · extract', icon: 'i-scan' },
    { id: 'policy', name: 'Policy Agent', role: 'Policy terms · claim history · rule engine', icon: 'i-shield' },
    { id: 'docs', name: 'Docs & Coding Agent', role: 'Package · required documents · benchmark', icon: 'i-clipboard' },
    { id: 'risk', name: 'Risk Agent', role: 'Rule signals · rationale', icon: 'i-radar' },
    { id: 'orchestrator', name: 'Orchestrator', role: 'Decision policy · grounded explanation', icon: 'i-flow' },
    { id: 'action', name: 'Action Agent', role: 'Action Gateway · letters · notices', icon: 'i-send' },
  ];
  const STATUS = {
    NEW: ['plain', 'New'], PROCESSING: ['gold', 'Agents working'], AWAITING_APPROVAL: ['gold', 'Awaiting sign-off'],
    ESCALATED: ['coral', 'With Medical Officer'], QUERY_RAISED: ['violet', 'Query to hospital'], APPROVED: ['teal', 'Approved'],
    PARTIALLY_APPROVED: ['sky', 'Partially approved'], DENIED: ['coral', 'Not approved'],
  };
  const STEP = {
    redact_phi: 'Redact PHI', injection_screen: 'Injection screen', extract_case: 'Extract case', coverage_review: 'Coverage review',
    rules_engine: 'Rule engine', coding_review: 'Coding review', documentation_check: 'Documentation check', cost_benchmark: 'Cost benchmark',
    rule_signals: 'Risk signals', rationale: 'Risk rationale', decision_policy: 'Decision policy', explain_decision: 'Explain decision',
    route_to_processor: 'Route to Processor', send_hospital_query: 'Query hospital', escalate_to_medical_officer: 'Escalate to MO',
    send_authorization_letter: 'Authorization letter', send_denial_letter: 'Decision letter', notify_hospital: 'Notify hospital',
    notify_patient: 'SMS patient', status_update: 'Status update', blocked: 'Blocked by gateway', start: 'Pipeline start', resume: 'Resume from saved state',
    settled: 'Settled', paused: 'Paused (waiting on hospital)', hospital_upload: 'Hospital upload', step_failed: 'Step failed', tool_call: 'Tool call',
  };
  const chip = (status) => { const [c, l] = STATUS[status] || ['plain', status]; return `<span class="chip ${c}"><span class="dot"></span>${l}</span>`; };
  const riskClass = (s) => (s == null ? '' : s >= 60 ? 'high' : s >= 30 ? 'medium' : 'low');
  const roleLabel = (r) => ({ PROCESSOR: 'Processor', MEDICAL_OFFICER: 'Medical Officer', AUDITOR: 'Auditor', ADMIN: 'Admin', SYSTEM: 'GoldenHour agent' }[r] || r);
  const modelChip = (m) => {
    if (!m) return '';
    if (m.startsWith('replay_miss')) return '<span class="k" style="color:var(--coral)">replay miss</span>';
    const [src, name] = m.includes(':') ? m.split(':') : ['', m];
    return `<span class="k">${esc((src ? src + ' · ' : '') + name.split('/').pop())}</span>`;
  };

  // ------------------------------------------------------------------ boot + header
  async function loadMeta() {
    S.meta = await api('GET', '/api/meta');
    const badge = $('#mode-badge');
    badge.className = `mode-badge ${S.meta.llm_mode}`;
    badge.textContent = S.meta.llm_mode;
    badge.title = `LLM mode: ${S.meta.llm_mode} (${S.meta.models.fast}, ${S.meta.models.reason}, ${S.meta.models.guard})`;
    const sel = $('#role');
    if (!sel.options.length) {
      sel.innerHTML = S.meta.roles.map((r) => `<option value="${r.id}">${esc(r.label)}${r.phi_access ? '' : ' (no PHI)'}</option>`).join('');
    }
    sel.value = GH.state.role;
    paintAutonomy();
  }
  function paintAutonomy() {
    const admin = GH.state.role === 'ADMIN';
    document.querySelectorAll('#autonomy button').forEach((b) => {
      b.setAttribute('aria-pressed', String(Number(b.dataset.level) === S.meta.autonomy_level));
      b.disabled = !admin;
      b.title = (admin ? '' : 'Admin only. ') + b.title.replace('Admin only. ', '');
    });
    $('#kill').disabled = !admin;
    $('#kill').setAttribute('aria-pressed', String(S.meta.kill_switch));
    $('#kill-banner').hidden = !S.meta.kill_switch;
    $('#kill-release').hidden = !admin;
  }
  function setRole(role) {
    GH.state.role = role;
    GH.store.set('gh.role', role);
    $('#role').value = role;
    paintAutonomy();
    refresh();
  }
  function bindHeader() {
    document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => {
      const tab = t.dataset.tab;
      location.hash = tab === 'case' && S.caseId ? `#/case/${S.caseId}` : `#/${tab}`;
    }));
    $('#role').addEventListener('change', (e) => setRole(e.target.value));
    document.querySelectorAll('#autonomy button').forEach((b) => b.addEventListener('click', async () => {
      try {
        await api('POST', '/api/admin/autonomy', { level: Number(b.dataset.level) });
        await loadMeta();
        toast(`Autonomy level set to ${b.dataset.level}${S.meta.kill_switch ? ' (kill switch still forces level 1)' : ''}.`);
        refresh();
      } catch (e) { toast(esc(e.message), 'err'); }
    }));
    const kill = async (engaged) => {
      try {
        await api('POST', '/api/admin/kill-switch', { engaged });
        await loadMeta();
        toast(engaged ? 'Kill switch engaged: autonomy forced to level 1.' : 'Kill switch released.', engaged ? 'err' : 'ok');
        refresh();
      } catch (e) { toast(`${e.status || ''} ${esc(e.message)}`, 'err'); }
    };
    $('#kill').addEventListener('click', () => kill(!S.meta.kill_switch));
    $('#kill-release').addEventListener('click', () => kill(false));
    const present = $('#present');
    present.setAttribute('aria-pressed', String(document.documentElement.classList.contains('presentation')));
    present.addEventListener('click', () => {
      const on = document.documentElement.classList.toggle('presentation');
      present.setAttribute('aria-pressed', String(on));
      GH.store.set('gh.present', on ? '1' : '0');
      if (S.tab === 'dashboard') renderDashboard();
    });
    $('#demo').addEventListener('click', () => guidedDemo());
  }

  function route() {
    const [tab, id] = (location.hash.replace(/^#\/?/, '') || 'inbox').split('/');
    S.tab = ['inbox', 'case', 'dashboard', 'audit'].includes(tab) ? tab : 'inbox';
    document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === S.tab)));
    document.querySelectorAll('.view').forEach((v) => { v.hidden = v.id !== `view-${S.tab}`; });
    if (S.tab !== 'case' && S.es) { S.es.close(); S.es = null; }
    if (S.tab === 'inbox') renderInbox();
    if (S.tab === 'case') openCase(id || S.caseId);
    if (S.tab === 'dashboard') renderDashboard();
    if (S.tab === 'audit') renderAudit();
    window.scrollTo({ top: 0 });
  }
  function refresh() {
    if (S.tab === 'inbox') renderInbox();
    if (S.tab === 'case' && S.caseId) reloadCase();
    if (S.tab === 'dashboard') renderDashboard();
    if (S.tab === 'audit') renderAudit();
  }

  // ------------------------------------------------------------------ inbox
  async function loadCases() {
    S.cases = await api('GET', '/api/cases');
    const waiting = S.cases.filter((c) => c.status === 'AWAITING_APPROVAL' || c.status === 'ESCALATED').length;
    const badge = $('#inbox-count');
    badge.hidden = !waiting;
    badge.textContent = waiting;
    return S.cases;
  }
  async function renderInbox() {
    const root = $('#view-inbox');
    if (!S.cases.length) root.innerHTML = `<div class="view-head"><div><h1>Inbox</h1></div></div>${[1, 2, 3, 4, 5].map(() => '<div class="skeleton" style="height:74px;margin-bottom:10px"></div>').join('')}`;
    try { await loadCases(); } catch (e) { root.innerHTML = errorCard(e, 'renderInbox'); return; }
    const count = (s) => S.cases.filter((c) => c.status === s).length;
    const need = count('AWAITING_APPROVAL') + count('ESCALATED');
    const decided = S.cases.filter((c) => c.decided_at);
    const order = { NEW: 0, PROCESSING: 1, ESCALATED: 2, AWAITING_APPROVAL: 3, QUERY_RAISED: 4 };
    const rows = [...S.cases].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || (a.hero || 'Z').localeCompare(b.hero || 'Z') || b.received_at - a.received_at);
    root.innerHTML = `
      <div class="view-head">
        <div><h1>Inbox</h1><p class="lede">${S.cases.length} cashless requests · <b>${need}</b> need a human now · ${decided.length} decided today · IRDAI window ${S.meta.sla_minutes} min</p></div>
        <div class="actions">
          <button class="btn" id="process-all">${icon('i-play', 'sm')}Process all new</button>
          <button class="btn ghost" id="reset">${icon('i-refresh', 'sm')}Reset demo</button>
        </div>
      </div>
      <div class="queue-strip">
        ${['NEW', 'AWAITING_APPROVAL', 'ESCALATED', 'QUERY_RAISED', 'APPROVED', 'PARTIALLY_APPROVED', 'DENIED'].map((s) => `<span class="chip ${STATUS[s][0]}">${STATUS[s][1]} <b class="num">${count(s)}</b></span>`).join('')}
      </div>
      ${rows.length ? `<div class="list-head"><span>SLA</span><span>Request</span><span>Patient</span><span class="r">Amount</span><span>Status</span><span>Risk</span><span></span></div>
      <div class="case-list">${rows.map(caseRow).join('')}</div>` : `<div class="card">${empty('i-inbox', 'No requests in the queue', 'New hospital requests will appear here as they arrive.')}</div>`}`;
    root.querySelectorAll('.case-row').forEach((row) => row.addEventListener('click', (e) => {
      if (e.target.closest('[data-process]')) return;
      location.hash = `#/case/${row.dataset.id}`;
    }));
    root.querySelectorAll('[data-process]').forEach((b) => b.addEventListener('click', async (e) => {
      e.stopPropagation();
      await processCase(b.dataset.process);
      location.hash = `#/case/${b.dataset.process}`;
    }));
    $('#process-all').addEventListener('click', async () => {
      const fresh = S.cases.filter((c) => c.status === 'NEW');
      for (const c of fresh) await processCase(c.case_id, true);
      toast(fresh.length ? `Started agents on ${fresh.length} case(s).` : 'No new cases to process.');
      setTimeout(renderInbox, 600);
    });
    $('#reset').addEventListener('click', resetDemo);
  }
  function caseRow(c) {
    const amount = c.status === 'DENIED' ? `${inr(c.requested_amount)}<small>requested · not approved</small>`
      : c.final_amount != null ? `${inr(c.final_amount)}<small>authorized of ${inr(c.requested_amount)}</small>`
      : c.recommended_amount != null && c.recommendation ? `${inr(c.requested_amount)}<small>${c.recommendation === 'RECOMMEND_APPROVE' ? 'eligible ' + inr(c.recommended_amount) : 'requested'}</small>`
      : `${inr(c.requested_amount)}<small>requested</small>`;
    const action = c.status === 'NEW' ? `<button class="btn sm primary" data-process="${c.case_id}">${icon('i-play', 'sm')}Process</button>`
      : `<button class="btn sm ghost">Open ${icon('i-arrow', 'sm')}</button>`;
    return `<div class="case-row ${c.hero ? 'hero' : ''}" data-id="${c.case_id}" tabindex="0" role="link" aria-label="Open ${c.case_id}">
      ${GH.dial(c.sla, 'sm')}
      <div><div class="title">${c.hero ? `<span class="hero-tag">${c.hero}</span>` : ''}<span class="mono muted">${c.case_id}</span> · ${esc(c.title)}</div>
        <div class="sub">${esc(c.hospital)}, ${esc(c.city)}</div></div>
      <div><div>${esc(c.patient)}</div><div class="sub">${c.age} ${c.gender} · received ${GH.ago(c.received_at)}</div></div>
      <div class="amt">${amount}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">${chip(c.running ? 'PROCESSING' : c.status)}${c.injection ? `<span class="chip coral" title="Prompt injection quarantined">${icon('i-alert', 'sm')}Injection</span>` : ''}${c.override ? '<span class="chip plain">Override</span>' : ''}</div>
      <div><span class="risk-pill ${riskClass(c.risk_score)}">${c.risk_score ?? '—'}</span></div>
      <div class="open">${action}</div></div>`;
  }
  async function processCase(id, quiet = false) {
    try { await api('POST', `/api/cases/${id}/process`); if (!quiet) toast(`Agents started on ${id}.`); }
    catch (e) { toast(`${e.status || ''} ${esc(e.message)}`, 'err'); }
  }
  async function resetDemo() {
    try {
      await api('POST', '/api/demo/reset', { history: true });
      S.cases = []; S.view = null; S.events = [];
      await loadMeta();
      toast('Demo reset: 10 synthetic requests in the inbox.');
      refresh();
    } catch (e) { toast(esc(e.message), 'err'); }
  }

  // ------------------------------------------------------------------ case view
  async function openCase(id) {
    const root = $('#view-case');
    if (!id) {
      root.innerHTML = `<div class="view-head"><div><h1>Case</h1><p class="lede">Pick a request from the inbox, or open a hero case.</p></div></div>
        <div class="card">${empty('i-file', 'No case selected', 'The live trace, decision card and outbox appear here.')}
        <div style="display:flex;gap:10px;justify-content:center;padding-bottom:32px">${(S.meta.hero_cases || []).map((h, i) => `<a class="btn" href="#/case/${h}">Case ${'ABC'[i]} · ${h}</a>`).join('')}</div></div>`;
      return;
    }
    if (S.caseId !== id) { S.events = []; S.view = null; S.docMode = 'original'; S.draft = {}; }
    S.caseId = id;
    if (!S.view) root.innerHTML = `<div class="skeleton" style="height:96px;margin-bottom:24px"></div><div class="case-grid"><div class="skeleton" style="height:520px"></div><div class="skeleton" style="height:520px"></div><div class="skeleton" style="height:520px"></div></div>`;
    try {
      const [view, events] = await Promise.all([api('GET', `/api/cases/${id}`), api('GET', `/api/cases/${id}/events`)]);
      S.view = view; S.events = events;
    } catch (e) { root.innerHTML = errorCard(e, null); return; }
    renderCaseShell();
    stream();
  }
  const signature = (v) => JSON.stringify({ ...v, sla: null, running: v.running && !v.decision, autonomy_level: v.autonomy_level, role: GH.state.role });
  async function reloadCase() {
    if (!S.caseId) return;
    try { S.view = await api('GET', `/api/cases/${S.caseId}`); } catch (e) { toast(esc(e.message), 'err'); return; }
    renderCaseParts();
  }
  function stream() {
    if (S.es) S.es.close();
    const last = S.events.length ? S.events[S.events.length - 1].id : 0;
    const es = new EventSource(`/api/cases/${S.caseId}/stream?after=${last}`);
    S.es = es;
    es.addEventListener('trace', (m) => {
      const ev = JSON.parse(m.data);
      if (ev.case_id !== S.caseId || S.events.some((e) => e.id === ev.id)) return;
      S.events.push(ev);
      renderTrace();
    });
    es.addEventListener('status', () => reloadCase());
    es.addEventListener('done', () => { es.close(); if (S.es === es) S.es = null; reloadCase(); });
    es.onerror = () => { es.close(); if (S.es === es) S.es = null; };
  }

  function renderCaseShell() {
    S.lastSig = null;
    $('#view-case').innerHTML = `<div id="c-head"></div><div id="c-quarantine"></div>
      <div class="case-grid"><div class="col trace-col"><div class="card trace" id="c-trace"></div></div>
      <div class="col" id="c-main"></div><div class="col" id="c-side"></div></div>`;
    renderCaseParts();
  }
  function renderCaseParts(force = false) {
    const v = S.view;
    if (!v || !$('#c-head')) return;
    renderHead(v);
    renderTrace();
    const sig = signature(v);
    if (!force && sig === S.lastSig) return;  // unchanged: keep inputs, scroll and animations as they are
    S.lastSig = sig;
    $('#c-quarantine').innerHTML = v.guard && v.guard.detected ? quarantineBanner(v) : '';
    renderMain(v);
    renderSide(v);
  }

  function renderHead(v) {
    const replayMiss = S.events.some((e) => (e.model || '').startsWith('replay_miss'));
    const dialCaption = v.sla.done ? `Decided in ${Math.max(1, Math.round(v.sla.elapsed_s / 60))} min against the ${S.meta.sla_minutes}-minute IRDAI window`
      : v.sla.paused ? 'SLA clock paused while the hospital uploads documents' : `of the ${S.meta.sla_minutes}-minute IRDAI cashless window`;
    $('#c-head').innerHTML = `<div class="case-head">
      <div class="ids"><div class="crumb"><a href="#/inbox">${icon('i-inbox', 'sm')} Inbox</a><span>/</span><span class="mono">${v.case_id}</span>
        ${v.hero ? `<span class="hero-tag">Case ${v.hero}</span>` : ''}${v.seeded ? '<span class="chip plain">seeded history</span>' : ''}</div>
        <h1>${esc(v.title)}</h1>
        <div class="meta"><span>${icon('i-human', 'sm')} ${esc(v.patient)}, ${v.age} ${v.gender}</span><span>${esc(v.hospital)}, ${esc(v.city)}</span>
          <span class="mono">${esc(v.form.policy_number)}</span>${v.policy ? `<span>${esc(v.policy.product)}</span>` : ''}
          ${chip(v.running ? 'PROCESSING' : v.status)}${replayMiss ? '<span class="chip coral">replay miss · mock fallback</span>' : ''}</div></div>
      <div class="dial-wrap"><div class="dial-caption">${dialCaption}</div>${GH.dial(v.sla, 'lg')}</div></div>`;
  }

  function quarantineBanner(v) {
    const s = (v.guard.sentences || []).map((x) => x.text);
    return `<div class="quarantine" role="alert">${icon('i-alert')}
      <div><strong>Prompt injection quarantined</strong>
      <span class="muted">Hidden text in the ${v.document && v.document.source === 'pdf' ? 'PDF' : 'note'} tried to instruct the model: </span>${s.map((t) => `<q>${esc(t)}</q>`).join(' ')}
      <div class="muted" style="font-size:.82rem;margin-top:4px">Removed before any model read it · ${esc(v.guard.method)} · score ${v.guard.score} · the case is escalated to a Medical Officer.</div></div>
      <span class="chip coral">Escalated</span></div>`;
  }

  // ---- live trace ----
  function lineDetail(e) {
    const o = e.output || {};
    switch (e.step) {
      case 'redact_phi': return Object.entries(o.redacted || {}).map(([k, n]) => `${n} ${k.toLowerCase()}`).join(' · ');
      case 'injection_screen': return o.detected ? `INJECTION · score ${o.score} · ${o.method}` : `clean · score ${o.score} · ${o.method}`;
      case 'extract_case': return `confidence ${o.confidence}${o.cross_check_issues && o.cross_check_issues.length ? ` · ${o.cross_check_issues.length} cross-check issue(s)` : ' · form cross-check clean'}`;
      case 'tool_call': return e.input_summary || '';
      case 'coverage_review': return o.claim_history_note || o.coverage_notes || '';
      case 'rules_engine': return `eligible ${o.eligible_amount}${o.deductions && o.deductions.length ? ' · ' + o.deductions.join(', ') : ''}${o.failed_rules && o.failed_rules.length ? ' · FAILED ' + o.failed_rules.join(', ') : ''}`;
      case 'coding_review': return `${o.procedure_key} · ${o.coding_consistent ? 'coding consistent' : 'coding inconsistent'}`;
      case 'documentation_check': return o.missing && o.missing.length ? `missing: ${o.missing.join(', ')}` : 'all required documents attached';
      case 'cost_benchmark': return `${o.requested} vs ${o.benchmark} (${o.deviation_pct > 0 ? '+' : ''}${o.deviation_pct}%)`;
      case 'rule_signals': return `score ${o.score} · ${o.level} · ${(o.signals || []).length} signal(s)`;
      case 'rationale': return o.rationale || '';
      case 'decision_policy': return `${o.outcome}${o.reasons && o.reasons.length ? ' · ' + o.reasons.length + ' reason(s)' : ''}`;
      case 'explain_decision': return o.grounded ? 'grounded in supplied rule results' : `rejected figures ${o.rejected_figures} → template`;
      case 'blocked': return `403 · ${o.reason || ''}`;
      case 'send_hospital_query': return `to ${o.to} · ${(o.missing || []).join(', ')} · SLA paused`;
      case 'hospital_upload': return `received ${(o.received || []).join(', ')} · SLA resumed`;
      case 'step_failed': return o.reason || '';
      case 'status_update': return `${o.status} · ${o.amount} · by ${o.by}`;
      case 'route_to_processor': return o.one_click ? `one-click sign-off, pre-filled ${o.prefilled_amount}` : 'manual review (level 1)';
      default: return typeof o === 'object' && o ? Object.entries(o).slice(0, 2).map(([k, x]) => `${k}: ${typeof x === 'object' ? JSON.stringify(x) : x}`).join(' · ') : '';
    }
  }
  function renderTrace() {
    const box = $('#c-trace');
    if (!box) return;
    const v = S.view || {};
    const running = v.running || v.status === 'PROCESSING';
    const byAgent = Object.fromEntries(AGENTS.map((a) => [a.id, []]));
    S.events.forEach((e) => { const key = e.agent === 'action_gateway' ? 'action' : e.agent; if (byAgent[key]) byAgent[key].push(e); });
    const lastAgent = [...S.events].reverse().find((e) => byAgent[e.agent === 'action_gateway' ? 'action' : e.agent]);
    const activeId = running && lastAgent ? (lastAgent.agent === 'action_gateway' ? 'action' : lastAgent.agent) : null;
    const tokens = S.events.reduce((n, e) => n + (e.tokens || 0), 0);
    box.innerHTML = `<div class="card-head"><h2>Live agent trace</h2><div class="right"><span class="chip mono plain">${S.events.length} events</span>${tokens ? `<span class="chip mono plain">${tokens.toLocaleString('en-IN')} tok</span>` : ''}</div></div>
      <div class="card-body">${AGENTS.map((a) => {
        const evs = byAgent[a.id];
        const alert = evs.some((e) => ['alert', 'error', 'blocked'].includes(e.status));
        const cls = [evs.length ? 'seen' : '', a.id === activeId ? 'active' : evs.length ? 'done' : '', alert ? 'alert' : ''].join(' ');
        return `<div class="agent ${cls}"><div class="a-head"><span class="a-icon">${icon(a.icon)}</span>
          <div><div class="a-name">${a.name}</div><div class="a-role">${a.role}</div></div>
          <span class="a-state">${evs.length && a.id !== activeId ? icon(alert ? 'i-alert' : 'i-check', 'sm') : ''}</span></div>
          ${evs.length ? `<ul class="lines">${evs.map((e) => `<li class="line ${e.status}"><span class="step">${esc(e.step === 'tool_call' ? e.tool : STEP[e.step] || e.step)}</span>
            ${e.tool && e.step !== 'tool_call' ? `<span class="tool">${esc(e.tool)}</span>` : e.step === 'tool_call' ? '<span class="tool">tool</span>' : ''}
            ${e.latency_ms ? `<span class="k">${e.latency_ms} ms</span>` : ''}${e.tokens ? `<span class="k">${e.tokens} tok</span>` : ''}${modelChip(e.model)}
            <span class="detail" title="${esc(lineDetail(e))}">${esc(lineDetail(e))}</span></li>`).join('')}</ul>` : ''}</div>`;
      }).join('')}
      ${!S.events.length ? `<div class="empty" style="padding:16px">${v.status === 'NEW' ? 'Agents have not run on this case yet.' : 'Waiting for events…'}</div>` : ''}</div>`;
    const body = box.querySelector('.card-body');
    if (running) body.scrollTop = body.scrollHeight;
  }

  // ---- main column: decision card, approval, outbox ----
  function renderMain(v) {
    const main = $('#c-main');
    if (v.status === 'NEW' || (!v.decision && (v.running || v.status === 'PROCESSING'))) {
      const working = v.running || v.status === 'PROCESSING';
      main.innerHTML = `<div class="card">${working
        ? `<div class="card-head"><h2>Decision card</h2><div class="right">${chip('PROCESSING')}</div></div><div class="card-body stack">
           <div class="skeleton" style="height:64px"></div><div class="skeleton" style="height:22px;width:70%"></div><div class="skeleton" style="height:180px"></div></div>`
        : `${empty('i-flow', 'Ready for the agents', 'Intake, Policy, Docs & Coding and Risk agents will run, then the orchestrator applies the decision policy.')}
           <div style="display:flex;justify-content:center;padding-bottom:32px"><button class="btn primary lg" id="run-case">${icon('i-play')}Run agents on ${v.case_id}</button></div>`}</div>`;
      const run = $('#run-case');
      if (run) run.addEventListener('click', async () => { await processCase(v.case_id); S.view.running = true; renderCaseParts(true); stream(); });
      return;
    }
    main.innerHTML = `${decisionCard(v)}<div class="card" id="c-approval">${approvalPanel(v)}</div>${outboxCard(v)}`;
    bindApproval(v);
  }

  function decisionCard(v) {
    const d = v.decision || {};
    const ev = (v.policy || {}).evaluation || { rules: [], deductions: [], failed_rules: [] };
    const x = v.explanation || {};
    const kind = { RECOMMEND_APPROVE: 'approve', QUERY_HOSPITAL: 'query', ESCALATE_MEDICAL_OFFICER: 'escalate' }[d.outcome] || 'escalate';
    const head = {
      approve: ['GoldenHour recommends', 'Approve', icon('i-check', 'lg')],
      query: ['Documents missing', 'Query sent to hospital', icon('i-mail', 'lg')],
      escalate: ['Routed to a human', 'Escalate to Medical Officer', icon('i-human', 'lg')],
    }[kind];
    const amount = kind === 'escalate' && ev.failed_rules.length
      ? `<div class="big">${inr(0)}</div><small>payable under policy rules<br>${inr(ev.amount_before_blocking)} if waived</small>`
      : `<div class="big">${inr(d.recommended_amount)}</div><small>eligible of ${inr(d.claimed_amount)} requested</small>`;
    const cited = new Set(x.cited_rules || []);
    return `<div class="card">
      <div class="outcome ${kind}"><span class="badge">${head[2]}</span><div><div class="label">${head[0]}</div><div class="headline">${head[1]}</div></div>
        <div class="amount">${amount}</div></div>
      ${x.summary ? `<div class="summary"><div>${esc(x.summary)}</div><div class="by">${icon('i-sparkle', 'sm')}Explanation by ${esc(sourceLabel(x.llm_source))}
        ${x.grounded ? '<span class="chip teal">grounded in rule results</span>' : '<span class="chip coral">unsupported figure rejected · template used</span>'}
        <span>· money math by rules.py, never the model</span></div></div>` : ''}
      ${d.reasons && d.reasons.length ? `<div class="summary"><div class="label muted" style="font-size:.72rem;letter-spacing:.1em;text-transform:uppercase">Why a human must decide</div>
        <ul class="reasons">${d.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul></div>` : ''}
      ${waterfall(ev)}
      <table class="ledger"><thead><tr><th>Rule</th><th>Clause</th><th>Result</th><th class="r">₹ impact</th></tr></thead><tbody>
      ${ev.rules.map((r) => {
        const result = !r.passed && r.blocking ? '<span class="chip coral">Fail</span>' : r.impact_inr < 0 ? '<span class="chip gold">Deduct</span>' : '<span class="chip teal">Pass</span>';
        return `<tr class="${!r.passed && r.blocking ? 'fail' : ''} ${cited.has(r.rule_id) ? 'cited' : ''}"><td><div class="rule">${esc(r.label)}</div><div class="why">${esc(r.explanation)}</div></td>
          <td class="clause">${esc(r.clause_ref)}</td><td>${result}</td><td class="r impact ${r.impact_inr < 0 ? 'neg' : ''}">${r.impact_inr ? inr(r.impact_inr) : '—'}</td></tr>`;
      }).join('')}</tbody></table></div>`;
  }
  function sourceLabel(src) {
    if (!src) return 'model';
    if (src === 'mock') return 'mock fixture';
    if (src === 'replay') return 'recorded Groq run (replay)';
    if (src === 'replay_miss') return 'mock fallback (replay miss)';
    return `Groq · ${S.meta.models.reason.split('/').pop()}`;
  }
  function waterfall(ev) {
    if (!ev.claimed_amount) return '';
    const max = ev.claimed_amount;
    const pct = (n) => `${Math.max(0, (n / max) * 100).toFixed(2)}%`;
    let level = max;
    const rows = [`<div class="wf-row"><span class="wf-label">Requested</span><div class="wf-track"><div class="wf-bar total" style="left:0;width:100%"></div></div><span class="wf-val">${inr(max)}</span></div>`];
    ev.deductions.forEach((dd, i) => {
      level -= dd.amount;
      rows.push(`<div class="wf-row"><span class="wf-label" title="${esc(dd.clause_ref)}">− ${esc(dd.label)}</span><div class="wf-track"><div class="wf-bar deduct" style="left:${pct(level)};width:${pct(dd.amount)};animation-delay:${120 * (i + 1)}ms"></div></div><span class="wf-val">${inr(-dd.amount)}</span></div>`);
    });
    if (ev.failed_rules.length) {
      rows.push(`<div class="wf-row"><span class="wf-label">Blocked by eligibility rule</span><div class="wf-track"><div class="wf-bar blocked" style="left:0;width:${pct(level)}"></div></div><span class="wf-val" style="color:var(--coral)">${inr(-level)}</span></div>`);
      rows.push(`<div class="wf-row pay"><span class="wf-label">Payable under rules</span><div class="wf-track"></div><span class="wf-val">${inr(0)}</span></div>`);
    } else {
      rows.push(`<div class="wf-row pay"><span class="wf-label">Payable</span><div class="wf-track"><div class="wf-bar pay" style="left:0;width:${pct(ev.eligible_amount)};animation-delay:${120 * (ev.deductions.length + 1)}ms"></div></div><span class="wf-val">${inr(ev.eligible_amount)}</span></div>`);
    }
    return `<div class="waterfall">${rows.join('')}</div>`;
  }

  function approvalPanel(v) {
    const d = v.decision || {};
    const role = GH.state.role;
    const level = S.meta.autonomy_level;
    const hd = v.human_decision;
    const head = (t, right = '') => `<div class="card-head"><h2>${t}</h2><div class="right">${right}</div></div>`;
    if (['APPROVED', 'PARTIALLY_APPROVED', 'DENIED'].includes(v.status) && hd) {
      const denied = v.status === 'DENIED';
      return `${head('Decision record', hd.override ? '<span class="chip gold">Human override</span>' : '')}<div class="record ${denied ? 'denied' : ''}">
        <span class="badge">${icon(denied ? 'i-x' : 'i-check')}</span><div><div><b>${STATUS[v.status][1]}${denied ? '' : ' · ' + inr(hd.amount)}</b>
        ${hd.auto ? ' · auto-executed at autonomy level 3' : ` · signed by ${roleLabel(hd.role)} <span class="mono muted">${esc(hd.actor)}</span>`} · ${GH.clock(hd.ts)}</div>
        ${hd.reason ? `<div class="muted" style="font-size:.86rem;margin-top:4px">Reason: ${esc(hd.reason)}</div>` : ''}
        ${hd.auto ? `<div class="muted" style="font-size:.84rem">${v.post_audit_sampled ? 'Sampled for post-audit (10% policy).' : 'Not in this post-audit sample.'}</div>` : ''}</div></div>`;
    }
    if (v.status === 'QUERY_RAISED') {
      const missing = (v.docs && v.docs.missing_labels) || [];
      return `${head('Waiting on the hospital', '<span class="chip violet">SLA paused</span>')}<div class="approval">
        <div class="hint">${icon('i-pause')}<span>GoldenHour queried ${esc(v.hospital)} for <b>${missing.map(esc).join(', ')}</b> on its own: low-risk and reversible. The case resumes from saved state when the document arrives.</span></div>
        <div class="row"><button class="btn primary" id="sim-upload" ${v.can.simulate_upload ? '' : 'disabled'}>${icon('i-upload', 'sm')}Simulate hospital upload</button>
        ${v.can.simulate_upload ? '' : `<span class="muted" style="font-size:.84rem">Not available to ${roleLabel(role)}.</span>`}</div></div>`;
    }
    if (v.status === 'AWAITING_APPROVAL') {
      if (!v.can.approve) return `${head('Sign-off')}<div class="approval"><div class="hint">${icon('i-lock')}<span>${roleLabel(role)} has read-only access. A Processor or Medical Officer signs this case.</span></div></div>`;
      const oneClick = level === 2 && d.one_click;
      const mo = v.can.change_amount;
      return `${head('Sign-off', `<span class="chip plain">Autonomy level ${level}</span>`)}<div class="approval">
        ${oneClick ? `<div class="row"><button class="btn primary lg" id="approve" data-amount="${d.recommended_amount}">${icon('i-check')}Approve ${inr(d.recommended_amount)}</button>
            ${v.can.escalate ? `<button class="btn" id="escalate">${icon('i-human', 'sm')}Escalate to Medical Officer</button>` : ''}</div>
            <div class="hint">${icon('i-human')}<span>Pre-filled by GoldenHour at autonomy level 2. Your one-click sign-off is written to the audit chain.</span></div>`
          : `<div class="field"><label for="amt">Amount to authorize</label><input class="input amount" id="amt" inputmode="numeric" placeholder="Type the amount, e.g. ${d.recommended_amount}" value="${S.draft.amount ?? ''}"></div>
            <label class="hint"><input type="checkbox" id="reviewed"> I reviewed the rule ledger and documents.</label>
            <div class="row"><button class="btn primary" id="approve-manual">${icon('i-check', 'sm')}Approve</button>${v.can.escalate ? `<button class="btn" id="escalate">Escalate to Medical Officer</button>` : ''}</div>
            <div class="hint">${icon('i-lock')}<span>Autonomy level 1${S.meta.kill_switch ? ' (kill switch)' : ''}: no pre-filled decision. Eligible amount under the rules is ${inr(d.recommended_amount)}.</span></div>`}
        ${mo ? moControls(v, d.recommended_amount) : ''}</div>`;
    }
    if (v.status === 'ESCALATED') {
      if (role === 'MEDICAL_OFFICER') {
        const ev = (v.policy || {}).evaluation || {};
        const suggested = d.recommended_amount || ev.amount_before_blocking || 0;
        return `${head('Medical Officer review', '<span class="chip coral">Assigned to you</span>')}<div class="approval">
          <div class="hint">${icon('i-shield')}<span>The AI cannot deny. Review the quarantined text, rule ledger and risk signals, then decide. A written reason is mandatory and goes into the audit chain.</span></div>
          ${moControls(v, suggested, true)}</div>`;
      }
      return `${head('Medical Officer review', '<span class="chip coral">Assigned to Medical Officer</span>')}<div class="approval">
        <div class="hint">${icon('i-lock')}<span>Escalated cases can only be decided by a Medical Officer. Any other attempt is rejected by the Action Gateway and logged.</span></div>
        <div class="row"><button class="btn" id="try-approve">${icon('i-lock', 'sm')}Approve ${inr(d.recommended_amount || ((v.policy || {}).evaluation || {}).amount_before_blocking)} (Medical Officer only)</button></div></div>`;
    }
    return `${head('Sign-off')}<div class="approval"><div class="hint">${icon('i-clock')}<span>No action needed right now.</span></div></div>`;
  }
  function moControls(v, amount, escalated = false) {
    return `<div class="field"><label for="mo-amt">${escalated ? 'Amount if approved' : 'Change the amount (Medical Officer)'}</label>
        <input class="input amount" id="mo-amt" inputmode="numeric" value="${S.draft.moAmount ?? amount}"></div>
      <div class="field"><label for="mo-reason">Written reason ${escalated ? '(required)' : '(required for a changed amount or a denial)'}</label>
        <textarea class="textarea" id="mo-reason" placeholder="e.g. Joint replacement has a 24-month waiting period (Sec 3.3); the policy is 8 months old.">${esc(S.draft.reason || '')}</textarea></div>
      <div class="row"><button class="btn ${escalated ? 'primary' : ''}" id="mo-approve">${icon('i-check', 'sm')}Approve amount</button>
        <button class="btn danger" id="mo-deny">${icon('i-x', 'sm')}Deny with reason</button></div>`;
  }
  function bindApproval(v) {
    const on = (id, fn) => { const el = $(`#${id}`); if (el) el.addEventListener('click', fn); };
    const keep = (id, key) => { const el = $(`#${id}`); if (el) el.addEventListener('input', () => { S.draft[key] = el.value; }); };
    keep('mo-reason', 'reason'); keep('mo-amt', 'moAmount'); keep('amt', 'amount');
    const num = (id) => Number(String(($(`#${id}`) || {}).value || '').replace(/[^\d]/g, ''));
    on('approve', (e) => decide('approve', { amount: Number(e.currentTarget.dataset.amount) }));
    on('approve-manual', () => {
      if (!$('#reviewed').checked) { toast('Tick the review box first: at level 1 the sign-off is fully manual.', 'err'); return; }
      decide('approve', { amount: num('amt') });
    });
    on('escalate', () => decide('escalate', { reason: 'Processor requested Medical Officer review' }));
    on('mo-approve', () => decide('approve', { amount: num('mo-amt'), reason: ($('#mo-reason') || {}).value || '' }));
    on('mo-deny', () => decide('deny', { reason: ($('#mo-reason') || {}).value || '' }));
    on('try-approve', () => decide('approve', {}));
    on('sim-upload', async () => {
      try {
        const r = await api('POST', `/api/cases/${v.case_id}/simulate-upload`);
        toast(`Hospital uploaded: ${r.received.join(', ')}. Resuming from saved state.`);
        S.view.running = true; S.view.status = 'PROCESSING'; renderCaseParts(true); stream();
      } catch (e) { toast(`${e.status || ''} ${esc(e.message)}`, 'err'); }
    });
  }
  async function decide(action, body) {
    try {
      const r = await api('POST', `/api/cases/${S.caseId}/decision`, { action, ...body });
      S.draft = {};
      toast(action === 'escalate' ? 'Escalated to the Medical Officer.' : `${STATUS[r.status] ? STATUS[r.status][1] : r.status}${r.amount ? ' · ' + inr(r.amount) : ''}. Outbox updated.`);
      await reloadCase();
      stream();
      return r;
    } catch (e) {
      if (e.status === 403) toast(`<b>403 · Blocked by the Action Gateway.</b> ${esc(e.message)}. The attempt is logged in the audit chain.`, 'err', 6500);
      else toast(`${e.status || ''} ${esc(e.message)}`, 'err', 6000);
      await reloadCase();
      return null;
    }
  }

  function outboxCard(v) {
    const rows = v.outbox || [];
    if (!rows.length) return '';
    const letter = rows.find((o) => o.kind === 'authorization_letter' || o.kind === 'denial_letter');
    const emails = rows.filter((o) => o.channel === 'email');
    const sms = rows.find((o) => o.kind === 'patient_sms');
    return `<div class="card" id="c-outbox"><div class="card-head"><h2>Outbox</h2><div class="right"><span class="chip plain">${rows.length} message${rows.length > 1 ? 's' : ''} via Action Gateway</span>
      ${v.phi_access ? '<span class="chip teal">rehydrated locally</span>' : '<span class="chip plain">PHI masked for your role</span>'}</div></div>
      <div class="card-body outbox"><div class="outbox-grid"><div class="stack">${letter ? letterHtml(letter) : ''}${emails.map(emailHtml).join('')}</div>${sms ? phoneHtml(sms, v) : ''}</div></div></div>`;
  }
  function letterHtml(o) {
    const b = o.body;
    const denial = b.type === 'denial';
    return `<article class="letter ${denial ? 'denial' : ''}" aria-label="${denial ? 'Decision letter' : 'Authorization letter'}">
      <div class="lh"><div><div class="org">${esc(b.letterhead)}</div><div class="note">${esc(b.desk)} · ${esc(b.letterhead_note)}</div></div>
        <div class="ref">${esc(b.reference)}<br>${esc(b.date)}</div></div>
      <div class="to">To: ${esc(b.to.attn)}, ${esc(b.to.name)}, ${esc(b.to.city)} · ${esc(b.to.code)}</div>
      <h4>${denial ? 'Cashless pre-authorization: not approved' : 'Cashless pre-authorization: approved'}</h4>
      <div class="grid2"><span><b>Patient</b> ${esc(b.patient)}</span><span><b>Policy</b> ${esc(b.policy_number)}</span>
        <span><b>Procedure</b> ${esc(b.procedure)}</span><span><b>Admission</b> ${esc(b.admission_date)} · ${esc(b.room_category)}</span></div>
      <table>${b.lines.map((l) => `<tr class="${l.total ? 'total' : ''}"><td>${esc(l.label)}</td><td class="r">${inr(l.amount)}</td></tr>`).join('')}</table>
      ${denial ? `<p><b>Reason.</b> ${esc(b.reason)}</p>` : ''}
      <ul>${b.conditions.map((c) => `<li>${esc(c)}</li>`).join('')}</ul>
      <div class="sig"><div>Signed digitally by ${esc(b.signed_by.role)} <span class="mono">${esc(b.signed_by.actor)}</span><br>Case ${esc(b.case_id)} · audit-chained</div>
        <span class="stamp">${denial ? 'Not approved' : 'Authorized'}</span></div></article>`;
  }
  function emailHtml(o) {
    const b = o.body;
    return `<div class="email"><div class="e-head"><span>${icon('i-mail', 'sm')} <b>${o.kind === 'hospital_query' ? 'Hospital query' : 'Hospital notice'}</b> · ${GH.clock(o.created_at)}</span>
      <span>From ${esc(b.from)} · To ${esc(b.to)}</span><span class="e-subj">${esc(b.subject)}</span></div>
      <div class="e-body">${esc(b.text)}</div>
      ${(b.attachments || []).length ? `<div class="e-att">${b.attachments.map((a) => `<span class="chip plain mono">${icon('i-file', 'sm')}${esc(a)}</span>`).join('')}</div>` : ''}
      ${(b.missing || []).length ? `<div class="e-att">${b.missing.map((m) => `<span class="chip violet">${esc(m.label)}</span>`).join('')}</div>` : ''}</div>`;
  }
  function phoneHtml(o) {
    return `<div class="phone" aria-label="Patient SMS"><div class="screen"><span class="notch"></span>
      <div class="sender"><b>${esc(o.body.sender)}</b>${esc(o.body.to)}</div><div class="bubble">${esc(o.body.text)}</div>
      <div class="time">Delivered · ${GH.clock(o.created_at)}</div></div></div>`;
  }

  // ---- side column: document, risk, docs & coding, intake ----
  function renderSide(v) {
    const side = $('#c-side');
    if (!v.document) {
      side.innerHTML = `<div class="card">${empty('i-redact', 'Document not read yet', 'The Intake agent redacts PHI and screens for prompt injection before any model sees the note.')}</div>`;
      return;
    }
    side.innerHTML = `${documentCard(v)}${riskCard(v)}${docsCard(v)}${intakeCard(v)}`;
    const hit = side.querySelector('.paper .inj, .paper .qtok');
    if (hit) { const paper = hit.closest('.paper'); paper.scrollTop = Math.max(0, hit.offsetTop - paper.clientHeight / 2); }
    side.querySelectorAll('.doc-toggle button').forEach((b) => b.addEventListener('click', () => { S.docMode = b.dataset.mode; renderSide(S.view); }));
  }
  function documentCard(v) {
    const doc = v.document;
    const model = S.docMode === 'model';
    const segs = model ? doc.model : doc.original;
    const html = segs.map((s) => {
      const t = esc(s.t);
      if (model) {
        if (s.inj) return `<span class="qtok">${t}</span>`;
        if (s.phi) return `<span class="tok">${t}</span>`;
        return t;
      }
      let out = s.phi ? `<span class="phi ${v.phi_access ? 'reveal' : ''}" title="${esc(s.token)}">${t}</span>` : t;
      if (s.inj) out = `<span class="inj">${out}</span>`;
      return out;
    }).join('');
    const counts = Object.entries(doc.phi_counts || {}).map(([k, n]) => `${n} ${k.toLowerCase()}`).join(' · ');
    return `<div class="card"><div class="card-head"><h3>${icon('i-redact', 'sm')} Admission note</h3><div class="right"><span class="chip plain mono">${esc(doc.filename)}</span>
      <div class="doc-toggle" role="group" aria-label="Document view"><button data-mode="original" aria-pressed="${!model}">Document</button><button data-mode="model" aria-pressed="${model}">What the model saw</button></div></div></div>
      <div class="card-body"><div class="paper doc"><span class="fold"></span>${html}</div>
      <div class="doc-legend">${model ? `<span><i style="background:rgba(245,185,66,.5)"></i>token (rehydrated locally)</span><span><i style="background:#D3264F"></i>quarantined</span>`
        : `<span><i style="background:#14120F"></i>PHI redacted${v.phi_access ? ' · hover to reveal' : ` · masked for ${roleLabel(GH.state.role)}`}</span>${v.guard && v.guard.detected ? '<span><i style="background:rgba(251,113,133,.5)"></i>injection</span>' : ''}`}
        <span class="faint">${counts}</span></div></div></div>`;
  }
  function riskCard(v) {
    const r = v.risk;
    if (!r) return '';
    const cls = riskClass(r.score);
    return `<div class="card"><div class="card-head"><h3>${icon('i-radar', 'sm')} Risk</h3><div class="right"><span class="chip ${cls === 'high' ? 'coral' : cls === 'medium' ? 'gold' : 'teal'}">${r.level}</span></div></div>
      <div class="card-body"><div class="gauge"><span class="score" style="color:${cls === 'high' ? 'var(--coral)' : cls === 'medium' ? 'var(--accent-text)' : 'var(--teal)'}">${r.score}</span>
        <div class="bar"><i style="left:${r.score}%"></i></div><span class="muted mono" style="font-size:.72rem">/100</span></div>
      ${r.signals.length ? `<ul class="signals">${r.signals.map((s) => `<li><span>${esc(s.label)}</span><span class="w">+${s.weight}</span><small>${esc(s.detail)}</small></li>`).join('')}</ul>` : '<p class="muted" style="margin:12px 0 0">No risk signals fired.</p>'}
      <div class="rationale">${esc(r.rationale)}<div class="faint" style="font-size:.72rem;margin-top:6px">Rationale by ${esc(sourceLabel(r.llm_source))} · score from weighted rules in pack.yaml</div></div></div></div>`;
  }
  function docsCard(v) {
    const d = v.docs;
    if (!d) return '';
    const dev = d.cost_deviation_pct;
    const requested = v.requested_amount;
    const scale = Math.max(requested, d.benchmark_rate_inr) * 1.1;
    return `<div class="card"><div class="card-head"><h3>${icon('i-clipboard', 'sm')} Docs & coding</h3><div class="right">${d.coding_consistent ? '<span class="chip teal">coding consistent</span>' : '<span class="chip coral">coding issue</span>'}</div></div>
      <div class="card-body"><div style="margin-bottom:10px"><b>${esc(d.procedure_name)}</b> <span class="chip plain mono">${esc(v.form.icd10)}</span></div>
      <ul class="checklist">${d.required_docs.map((r) => `<li class="${r.attached ? '' : 'missing'}">${r.attached ? `<span class="ok">${icon('i-check', 'sm')}</span>` : `<span class="no">${icon('i-x', 'sm')}</span>`}${esc(r.label)}</li>`).join('')}</ul>
      <div class="bench"><div style="display:flex;justify-content:space-between"><span class="muted">Requested vs package benchmark</span><b style="color:${dev > 25 ? 'var(--coral)' : 'var(--text)'}">${dev > 0 ? '+' : ''}${dev}%</b></div>
        <div class="track"><div class="fill ${dev > 25 ? 'over' : ''}" style="width:${(requested / scale) * 100}%"></div><div class="mark" style="left:${(d.benchmark_rate_inr / scale) * 100}%" title="benchmark"></div></div>
        <div style="display:flex;justify-content:space-between" class="muted"><span>${inr(requested)} requested</span><span>${inr(d.benchmark_rate_inr)} benchmark · LOS ${v.form.expected_los_days}/${d.typical_los_days} d</span></div></div></div></div>`;
  }
  function intakeCard(v) {
    const i = v.intake;
    if (!i) return '';
    return `<div class="card"><div class="card-head"><h3>${icon('i-scan', 'sm')} Intake extraction</h3><div class="right"><span class="chip ${i.confidence >= 0.8 ? 'teal' : 'coral'}">confidence ${i.confidence}</span></div></div>
      <div class="card-body"><dl class="kv"><dt>Extracted by</dt><dd>${esc(sourceLabel(i.llm_source).replace(S.meta.models.reason.split('/').pop(), S.meta.models.fast.split('/').pop()))}</dd>
        <dt>Diagnosis</dt><dd>${esc(i.extraction.provisional_diagnosis)}</dd><dt>ICD-10</dt><dd class="mono">${esc(i.extraction.icd10_codes.join(', '))}</dd>
        <dt>Stay</dt><dd>${i.extraction.expected_los_days} days · ${inr(i.extraction.room_rent_per_day)}/day</dd>
        <dt>Form cross-check</dt><dd>${i.cross_check_issues.length ? `<span style="color:var(--coral)">${i.cross_check_issues.map(esc).join('; ')}</span>` : 'matches the hospital form'}</dd></dl></div></div>`;
  }

  // ------------------------------------------------------------------ dashboard
  async function renderDashboard() {
    const root = $('#view-dashboard');
    if (!root.innerHTML) root.innerHTML = `<div class="kpis">${[1, 2, 3, 4, 5].map(() => '<div class="skeleton" style="height:150px"></div>').join('')}</div><div class="skeleton" style="height:360px"></div>`;
    let d;
    try { d = await api('GET', '/api/dashboard'); } catch (e) { root.innerHTML = errorCard(e, null); return; }
    const k = d.kpis;
    root.innerHTML = `<div class="view-head"><div><h1>Dashboard</h1><p class="lede">Turnaround, decisions and value, computed live from the case store. Synthetic data.</p></div>
      <div class="actions"><span class="chip plain">IRDAI cashless window ${d.sla_minutes} min</span><span class="chip ${d.kill_switch ? 'coral' : 'gold'}">Autonomy level ${d.autonomy_level}${d.kill_switch ? ' · kill switch' : ''}</span></div></div>
      <div class="kpis">
        ${kpi('Median turnaround', 'k-tat', 'min', `decision time vs the ${d.sla_minutes}-minute rule`, 'accent', 'i-clock')}
        ${kpi('Within 60-minute SLA', 'k-sla', '%', `${k.decided} decided cases`, '', 'i-check')}
        ${kpi('Cases processed', 'k-proc', '', 'by the agent pipeline', '', 'i-flow')}
        ${kpi('Human override rate', 'k-ovr', '%', `${k.overrides} of ${k.human_reviews} human reviews`, '', 'i-human')}
        ${kpi('Processor hours saved', 'k-hrs', 'h', `${d.roi.saved_minutes_per_case} min saved per case`, '', 'i-sparkle')}
      </div>
      <div class="dash-grid">
        <div class="card"><div class="card-head"><h2>Turnaround per decided case</h2><div class="right"><span class="chip gold">— 60-minute line</span></div></div><div class="card-body"><div class="chart-box"><canvas id="tat-chart" aria-label="Turnaround chart"></canvas></div></div></div>
        <div class="card"><div class="card-head"><h2>Decision split</h2></div><div class="card-body"><div class="chart-box donut"><canvas id="split-chart" aria-label="Decision split"></canvas></div><div class="legend" id="split-legend"></div></div></div>
      </div>
      <div class="tiles">
        ${tile(k.queries_sent, 'Hospital queries sent autonomously', 'i-mail')}
        ${tile(k.injections_blocked, 'Prompt injections quarantined', 'i-alert')}
        ${tile(k.unauthorized_blocked, 'Unauthorized actions blocked (403)', 'i-lock')}
        ${tile(inr(k.deductions_inr), 'Policy deductions identified and cited', 'i-shield')}
      </div>
      <div class="card" style="margin-top:24px"><div class="card-head"><h2>AI-to-ROI</h2></div><div class="card-body roi">
        <div><div class="muted" style="font-size:.84rem">Projected processor hours saved per year at ${d.roi.daily_volume} cases/day</div>
          <div class="big" id="k-annual">0</div>
          <div class="muted" style="font-size:.84rem;margin-top:6px">≈ <b style="color:var(--text)">${inr(d.roi.annual_inr)}</b> a year at ${inr(d.roi.processor_cost_per_hour_inr)}/hour · assumptions: ${d.roi.manual_minutes_per_case} min manual vs ${d.roi.assisted_minutes_per_case} min assisted per case, ${d.roi.working_days_per_year} working days (pack.yaml)</div></div>
        <div class="compare"><div class="muted" style="font-size:.8rem">Time to a cashless decision</div>
          <div class="bar-row"><span>Typical today</span><div class="t"><i style="width:100%;background:var(--coral)"></i></div><span class="num">6–48 h</span></div>
          <div class="bar-row"><span>IRDAI mandate</span><div class="t"><i style="width:${(60 / (48 * 60)) * 100 * 8}%;background:var(--accent)"></i></div><span class="num">60 min</span></div>
          <div class="bar-row"><span>GoldenHour median</span><div class="t"><i style="width:${Math.max(1.5, ((k.median_tat_min || 0) / (48 * 60)) * 100 * 8)}%;background:var(--teal)"></i></div><span class="num">${k.median_tat_min ?? '—'} min</span></div>
          <div class="faint" style="font-size:.74rem">${esc(d.baseline.label)}. Bars after the first are drawn 8x for legibility.</div></div></div></div>`;
    countUp($('#k-tat'), k.median_tat_min, { decimals: 1 });
    countUp($('#k-sla'), k.within_sla_pct);
    countUp($('#k-proc'), k.processed);
    countUp($('#k-ovr'), k.override_rate_pct);
    countUp($('#k-hrs'), k.hours_saved, { decimals: 1 });
    countUp($('#k-annual'), d.roi.annual_hours, { format: (v) => Math.round(v).toLocaleString('en-IN') + '<small style="font-size:1.2rem;color:var(--muted)"> h</small>' });
    drawCharts(d);
  }
  const kpi = (label, id, unit, note, cls, ic) => `<div class="card kpi ${cls}"><div class="k-label">${icon(ic, 'sm')}${label}</div>
    <div class="k-value"><span id="${id}">0</span>${unit ? `<small>${unit}</small>` : ''}</div><div class="k-note">${note}</div></div>`;
  const tile = (value, label, ic) => `<div class="card tile"><div class="t-val">${value}</div><div class="t-label">${icon(ic, 'sm')} ${label}</div></div>`;
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function drawCharts(d) {
    if (!window.Chart) return;
    Object.values(S.charts).forEach((c) => c.destroy());
    const text = cssVar('--muted'), line = cssVar('--line'), teal = cssVar('--teal'), coral = cssVar('--coral'), gold = cssVar('--accent');
    Chart.defaults.font.family = 'Inter, system-ui, sans-serif';
    Chart.defaults.color = text;
    const labels = d.tat.map((t) => t.case_id);
    const max = Math.max(70, ...d.tat.map((t) => t.minutes + 8));
    S.charts.tat = new Chart($('#tat-chart'), {
      data: {
        labels,
        datasets: [
          { type: 'bar', label: 'Turnaround (min)', data: d.tat.map((t) => t.minutes), borderRadius: 6, maxBarThickness: 46, minBarLength: 5,
            backgroundColor: d.tat.map((t) => (t.breached ? coral : teal) + (t.seeded ? '99' : '')), order: 2 },
          { type: 'line', label: 'IRDAI 60-minute limit', data: labels.map(() => d.sla_minutes), borderColor: gold, borderDash: [6, 6], borderWidth: 2, pointRadius: 0, order: 1 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: GH.reduced ? 0 : 700 },
        plugins: { legend: { display: false }, tooltip: { callbacks: { afterLabel: (c) => (c.datasetIndex === 0 ? d.tat[c.dataIndex].title + (d.tat[c.dataIndex].seeded ? ' (seeded history)' : ' (live)') : '') } } },
        scales: { y: { beginAtZero: true, max, grid: { color: line }, title: { display: true, text: 'minutes' } }, x: { grid: { display: false }, ticks: { font: { family: 'JetBrains Mono', size: 11 } } } },
      },
    });
    const colors = { Approved: teal, 'Partially approved': cssVar('--sky'), 'Denied by MO': coral, 'With Medical Officer': '#F59E9E', 'Query to hospital': cssVar('--violet'), 'Awaiting sign-off': gold };
    const entries = Object.entries(d.split).filter(([, n]) => n > 0);
    S.charts.split = new Chart($('#split-chart'), {
      type: 'doughnut',
      data: { labels: entries.map(([l]) => l), datasets: [{ data: entries.map(([, n]) => n), backgroundColor: entries.map(([l]) => colors[l]), borderColor: cssVar('--surface'), borderWidth: 3 }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: '68%', animation: { duration: GH.reduced ? 0 : 700 }, plugins: { legend: { display: false } } },
    });
    $('#split-legend').innerHTML = entries.map(([l, n]) => `<div><i style="background:${colors[l]}"></i>${l}<b>${n}</b></div>`).join('');
  }

  // ------------------------------------------------------------------ audit chain
  async function renderAudit(result = null) {
    const root = $('#view-audit');
    let blocks;
    try { blocks = await api('GET', '/api/audit'); } catch (e) { root.innerHTML = errorCard(e, null); return; }
    const admin = GH.state.role === 'ADMIN';
    const banner = result ? (result.valid
      ? `<div class="verify-banner ok">${icon('i-shield')}<div><b>Chain verified.</b> ${result.checked} blocks recomputed: every hash = sha256(prev_hash + payload) matches.</div></div>`
      : `<div class="verify-banner bad">${icon('i-alert')}<div><b>Verification failed at block #${result.broken_at}.</b> ${esc(result.reason)}. Every block after it is unverified.</div>${admin ? `<button class="btn sm" id="restore" style="margin-left:auto">${icon('i-refresh', 'sm')}Restore</button>` : ''}</div>`)
      : `<div class="verify-banner">${icon('i-hash')}<div>${blocks.length} blocks. Each stores <span class="mono">hash = sha256(prev_hash + payload)</span>; editing any stored row breaks every link after it.</div></div>`;
    root.innerHTML = `<div class="view-head"><div><h1>Audit chain</h1><p class="lede">Every case receipt, agent step, gateway action, blocked attempt and human decision, tamper-evident.</p></div>
      <div class="actions"><button class="btn primary" id="verify">${icon('i-shield', 'sm')}Verify chain</button>
        <button class="btn danger" id="tamper" ${admin ? '' : 'disabled title="Admin only"'}>${icon('i-alert', 'sm')}Tamper test${admin ? '' : ' (Admin)'}</button>
        <button class="btn ghost" id="restore2" ${admin ? '' : 'disabled title="Admin only"'}>${icon('i-refresh', 'sm')}Restore</button></div></div>
      <div id="verify-slot">${banner}</div>
      ${blocks.length ? `<div class="chain" id="chain">${blocks.map(blockHtml).join('')}</div>` : `<div class="card">${empty('i-chain', 'The chain is empty', 'Reset the demo to seed cases.')}</div>`}`;
    $('#verify').addEventListener('click', verifyChain);
    $('#tamper').addEventListener('click', async () => {
      try {
        const t = await api('POST', '/api/audit/tamper');
        toast(`Tamper test: block #${t.tampered_block} was edited directly in SQLite (${esc(JSON.stringify(t.change).slice(0, 80))}). Now verify.`, 'err', 6500);
        await renderAudit();
      } catch (e) { toast(`${e.status || ''} ${esc(e.message)}`, 'err'); }
    });
    const restore = async () => { try { await api('POST', '/api/audit/restore'); toast('Original payload restored.'); await renderAudit(); } catch (e) { toast(esc(e.message), 'err'); } };
    $('#restore2').addEventListener('click', restore);
    if ($('#restore')) $('#restore').addEventListener('click', restore);
    if (result) paintVerify(result, false);
  }
  function blockHtml(b) {
    const human = ['approve', 'deny', 'partial_approve', 'escalate', 'simulate_upload', 'set_autonomy', 'kill_switch', 'tamper_test', 'audit_restore', 'process_requested'].includes(b.action) && b.role !== 'SYSTEM';
    const label = b.action.replace(/^step:/, 'agent · ').replace(/_/g, ' ');
    return `<div class="block ${b.action === 'action_blocked' ? 'blocked-act' : ''} ${human ? 'human' : ''}" data-id="${b.id}">
      <div class="b-top"><span class="b-id">#${b.id}</span><span class="b-state"></span></div>
      <div class="b-act" title="${esc(b.action)}">${esc(label)}</div>
      <div class="b-who">${esc(b.case_id || 'system')} · ${esc(roleLabel(b.role))}</div>
      <div class="b-hash"><span>prev ${b.prev_hash.slice(0, 6)}</span><b>${b.hash.slice(0, 8)}</b></div></div>`;
  }
  async function verifyChain() {
    try {
      const r = await api('POST', '/api/audit/verify');
      await paintVerify(r, true);
      $('#verify-slot').innerHTML = r.valid
        ? `<div class="verify-banner ok">${icon('i-shield')}<div><b>Chain verified.</b> ${r.checked} blocks recomputed: every hash = sha256(prev_hash + payload) matches.</div></div>`
        : `<div class="verify-banner bad">${icon('i-alert')}<div><b>Verification failed at block #${r.broken_at}.</b> ${esc(r.reason)}. Every block after it is unverified.</div>${GH.state.role === 'ADMIN' ? `<button class="btn sm" id="restore" style="margin-left:auto">${icon('i-refresh', 'sm')}Restore</button>` : ''}</div>`;
      const btn = $('#restore');
      if (btn) btn.addEventListener('click', async () => { await api('POST', '/api/audit/restore'); toast('Original payload restored.'); await renderAudit(); });
      return r;
    } catch (e) { toast(`${e.status || ''} ${esc(e.message)}`, 'err'); return null; }
  }
  async function paintVerify(r, animate) {
    const ok = Object.fromEntries(r.blocks.map((b) => [b.id, b.ok]));
    const els = [...document.querySelectorAll('.block')];
    els.forEach((el) => el.classList.remove('ok', 'broken', 'unverified'));
    let broken = false;
    const step = animate && !GH.reduced ? Math.max(6, Math.min(30, 2400 / els.length)) : 0;
    for (const el of els) {
      const id = Number(el.dataset.id);
      if (broken) { el.classList.add('unverified'); continue; }
      if (ok[id]) { el.classList.add('ok'); el.querySelector('.b-state').innerHTML = icon('i-check', 'sm'); }
      else if (id === r.broken_at) { broken = true; el.classList.add('broken'); el.querySelector('.b-state').innerHTML = icon('i-x', 'sm'); const r = el.getBoundingClientRect(); if (r.top < 70 || r.bottom > innerHeight) el.scrollIntoView({ block: 'center', behavior: GH.reduced ? 'auto' : 'smooth' }); }
      if (step) await sleep(step);
    }
  }

  // ------------------------------------------------------------------ helpers
  function empty(ic, title, text) {
    return `<div class="empty"><span class="ring">${icon(ic, 'lg')}</span><h3>${title}</h3><p style="margin:0;max-width:420px">${text}</p></div>`;
  }
  function errorCard(e, retry) {
    return `<div class="error-card">${icon('i-alert')}<div><b>Could not load this view.</b> <span class="muted">${esc(e.message)}</span></div>
      <button class="btn sm" style="margin-left:auto" onclick="location.reload()">Retry</button></div>`;
  }

  // ------------------------------------------------------------------ guided demo (A -> B -> C)
  async function waitFor(id, statuses, timeout = 240000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      const v = await api('GET', `/api/cases/${id}`);
      if (statuses.includes(v.status) && !v.running) return v;
      await sleep(500);
    }
    throw new Error(`${id} did not settle`);
  }
  async function watch(id) {
    location.hash = `#/case/${id}`;
    await sleep(700);
  }
  async function guidedDemo() {
    if (S.demo) return;
    S.demo = true;
    const btn = $('#demo');
    btn.disabled = true;
    const say = async (text, step, ms = 2600) => { caption(text, step); await sleep(ms); };
    try {
      setRole('PROCESSOR');
      await say('Resetting the desk: ten synthetic cashless requests arrive from three hospitals.', 'GoldenHour', 1800);
      await api('POST', '/api/demo/reset', { history: true });
      await loadMeta();
      location.hash = '#/inbox';
      await sleep(1600);

      await watch('PA-1001');
      await api('POST', '/api/cases/PA-1001/process');
      reloadCase(); stream();
      await say('Case A. The Intake agent redacts patient data before any model sees it, then screens for prompt injection.', 'A · intake', 3200);
      await say('The Policy agent pulls policy terms with tool calls. The rule engine, not the model, does the money math.', 'A · policy', 3000);
      await waitFor('PA-1001', ['AWAITING_APPROVAL', 'ESCALATED']);
      await reloadCase();
      await say('Room rent is above the cap: ₹9,000 deducted, cited to Sec 4.2. GoldenHour pre-fills an approval of ₹1,01,000.', 'A · decision card', 4200);
      await say('The Processor signs off with one click.', 'A · human sign-off', 1600);
      await decide('approve', { amount: S.view.decision.recommended_amount });
      $('#c-outbox') && $('#c-outbox').scrollIntoView({ behavior: 'smooth', block: 'start' });
      await say('Authorization letter, hospital notice and patient SMS go out through the Action Gateway, rehydrated locally.', 'A · outbox', 4200);

      await watch('PA-1002');
      await api('POST', '/api/cases/PA-1002/process');
      reloadCase(); stream();
      await say('Case B. Cataract surgery: the Docs & Coding agent finds the biometry (A-scan) report missing.', 'B · documents', 3000);
      await waitFor('PA-1002', ['QUERY_RAISED', 'AWAITING_APPROVAL', 'ESCALATED']);
      await reloadCase();
      await say('The agent queries the hospital on its own (low-risk, reversible) and pauses the SLA clock.', 'B · autonomous query', 3800);
      await say('The hospital uploads the report. The case resumes from saved state.', 'B · resume', 1800);
      await api('POST', '/api/cases/PA-1002/simulate-upload');
      reloadCase(); stream();
      await waitFor('PA-1002', ['AWAITING_APPROVAL', 'ESCALATED']);
      await reloadCase();
      await say('Approved within the ₹40,000 cataract sub-limit, less the 10% co-pay: ₹36,000.', 'B · sign-off', 2600);
      await decide('approve', { amount: S.view.decision.recommended_amount });
      await sleep(1600);

      await watch('PA-1003');
      await api('POST', '/api/cases/PA-1003/process');
      reloadCase(); stream();
      await say('Case C. Knee replacement on an 8-month-old policy. The PDF hides white text aimed at the model.', 'C · intake', 3200);
      await waitFor('PA-1003', ['ESCALATED', 'AWAITING_APPROVAL']);
      await reloadCase();
      await say('Injection quarantined, waiting-period rule failed, risk high: escalated to the Medical Officer.', 'C · quarantine', 4200);
      await say('A Processor tries to approve anyway…', 'C · RBAC', 1600);
      await decide('approve', {});
      await say('Blocked with 403 by the Action Gateway, and the attempt is written to the audit chain.', 'C · 403', 3000);
      setRole('MEDICAL_OFFICER');
      await sleep(900);
      S.draft.reason = 'Joint replacement has a 24-month waiting period (Sec 3.3) and the policy is 8 months old. Hidden instruction text in the note was disregarded.';
      renderCaseParts(true);
      await say('The Medical Officer reviews the evidence and records a written reason. The AI never denies.', 'C · human decision', 3400);
      await decide('deny', { reason: S.draft.reason || 'Waiting period not served (Sec 3.3).' });
      await sleep(2200);

      setRole('AUDITOR');
      location.hash = '#/audit';
      await sleep(1200);
      await say('Every step is hash-chained. Verify the chain…', 'Audit', 1200);
      await verifyChain();
      await say('Valid. An Admin-only tamper test would break the chain at the edited block.', 'Audit', 3200);
      location.hash = '#/dashboard';
      await say('Turnaround against the 60-minute line, decision split and processor hours saved.', 'Dashboard', 4500);
      caption('');
      toast('Guided demo complete: A approved, B approved after a hospital query, C decided by the Medical Officer.');
    } catch (e) {
      caption('');
      toast(`Guided demo stopped: ${esc(e.message)}`, 'err', 7000);
    } finally {
      S.demo = false;
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ start
  async function boot() {
    bindHeader();
    try { await loadMeta(); } catch (e) { $('#view-inbox').innerHTML = errorCard(e, null); return; }
    window.addEventListener('hashchange', route);
    route();
    if (new URLSearchParams(location.search).get('demo') === '1') {
      history.replaceState(null, '', '/console#/inbox');
      setTimeout(guidedDemo, 600);
    }
    setInterval(GH.tickDials, 1000);
    setInterval(() => { if (S.tab === 'inbox' && !document.hidden) renderInbox(); }, 5000);
    window.GoldenHour = { guidedDemo, setRole, state: S };
  }
  boot();
})();
