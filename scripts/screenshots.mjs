// Headless screenshots for docs/screenshots at 1440x900. Needs a running server (python run.py) and Playwright.
// Usage: node scripts/screenshots.mjs [base_url] [only-name-prefix]
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
let playwright;
try { playwright = require('playwright'); } catch { playwright = require('/opt/node-tools/node_modules/playwright'); }

const BASE = process.argv[2] || 'http://127.0.0.1:8000';
const ONLY = process.argv[3] || '';
const OUT = new URL('../docs/screenshots/', import.meta.url).pathname;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function api(method, path, role = 'PROCESSOR', body) {
  const r = await fetch(BASE + path, { method, headers: { 'Content-Type': 'application/json', 'X-Role': role }, body: body ? JSON.stringify(body) : undefined });
  return r.json();
}
async function wait(id, statuses) {
  for (let i = 0; i < 600; i++) {
    const v = await api('GET', `/api/cases/${id}`);
    if (statuses.includes(v.status) && !v.running) return v;
    await sleep(500);
  }
  throw new Error(`timeout waiting for ${id}`);
}

(async () => {
  await api('POST', '/api/demo/reset', 'ADMIN', { history: true });
  for (const id of ['PA-1001', 'PA-1002', 'PA-1003']) await api('POST', `/api/cases/${id}/process`);
  await wait('PA-1001', ['AWAITING_APPROVAL', 'ESCALATED']);
  await wait('PA-1002', ['QUERY_RAISED', 'AWAITING_APPROVAL']);
  await wait('PA-1003', ['ESCALATED']);

  const browser = await playwright.chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => console.log('PAGE ERROR', e.message));
  page.on('console', (m) => { if (m.type() === 'error') console.log('CONSOLE', m.text()); });
  const want = (name) => !ONLY || name.startsWith(ONLY);
  const shot = async (name, opts = {}) => { if (!want(name)) return; await sleep(1300); await page.screenshot({ path: OUT + name, ...opts }); console.log('saved', name); };
  const asRole = async (role) => { await page.goto(BASE + '/console#/inbox'); await page.evaluate((r) => { localStorage.setItem('gh.role', r); localStorage.setItem('gh.present', '0'); }, role); };

  for (let i = 0; i < 120; i++) { const d = await api('GET', '/api/day'); if (!d.active) break; await sleep(500); }  // the reset's 24-hour backfill is done
  await asRole('PROCESSOR');
  if (want('landing')) { await page.goto(BASE + '/'); await shot('landing.png'); await shot('landing-full.png', { fullPage: true }); }
  await page.goto(BASE + '/console#/inbox'); await page.reload(); await page.waitForSelector('.case-row'); await shot('inbox.png');

  await api('POST', '/api/cases/PA-1001/decision', 'PROCESSOR', { action: 'approve' });
  await page.goto(BASE + '/console#/case/PA-1001'); await page.waitForSelector('.ledger'); await shot('case-a.png'); await shot('case-a-full.png', { fullPage: true });

  await api('POST', '/api/cases/PA-1003/decision', 'PROCESSOR', { action: 'approve' }); // 403, logged
  await asRole('MEDICAL_OFFICER');
  await page.goto(BASE + '/console#/case/PA-1003'); await page.reload(); await page.waitForSelector('.quarantine'); await shot('case-c.png'); await shot('case-c-full.png', { fullPage: true });
  if (want('case-c-model')) { await page.click('.doc-toggle button[data-mode="model"]'); await shot('case-c-model.png'); }

  await asRole('PROCESSOR');
  await page.goto(BASE + '/console#/case/PA-1002'); await page.reload(); await page.waitForSelector('#sim-upload'); await shot('case-b-query.png');

  await api('POST', '/api/cases/PA-1002/simulate-upload', 'PROCESSOR');
  await wait('PA-1002', ['AWAITING_APPROVAL']);
  await api('POST', '/api/cases/PA-1002/decision', 'PROCESSOR', { action: 'approve' });
  await api('POST', '/api/cases/PA-1003/decision', 'MEDICAL_OFFICER', { action: 'deny', reason: 'Joint replacement has a 24-month waiting period (Sec 3.3) and the policy is 8 months old. Hidden instruction text in the note was disregarded.' });

  await page.goto(BASE + '/console#/dashboard'); await page.waitForSelector('#day-chart'); await shot('dashboard.png');
  if (want('day-replay')) { await page.click('#dash-replay'); await sleep(9000); await shot('day-replay.png'); }

  await asRole('AUDITOR');
  await page.goto(BASE + '/console#/audit'); await page.reload(); await page.waitForSelector('#verify');
  await page.click('#verify'); await page.waitForSelector('.verify-banner.ok', { timeout: 15000 }); await shot('audit.png');
  if (want('audit-tamper')) {
    await api('POST', '/api/audit/tamper', 'ADMIN');
    await page.reload(); await page.waitForSelector('#verify'); await page.click('#verify');
    await page.waitForSelector('.verify-banner.bad', { timeout: 15000 }); await shot('audit-tamper.png');
    await api('POST', '/api/audit/restore', 'ADMIN');
  }
  if (want('presentation')) {
    await asRole('PROCESSOR');
    await page.evaluate(() => localStorage.setItem('gh.present', '1'));
    await page.goto(BASE + '/console#/case/PA-1001'); await page.reload(); await page.waitForSelector('.ledger'); await shot('presentation.png');
    await page.evaluate(() => localStorage.setItem('gh.present', '0'));
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
