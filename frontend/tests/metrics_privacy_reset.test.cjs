const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
function extract(name) {
  const position = html.indexOf(`function ${name}(`);
  assert.ok(position !== -1, name);
  const start = html.slice(Math.max(0, position - 6), position) === 'async ' ? position - 6 : position;
  const open = html.indexOf('{', start);
  let depth = 0, quote = '', escaped = false;
  for (let i = open; i < html.length; i++) {
    const c = html[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (c === '\\') escaped = true;
      else if (c === quote) quote = '';
    } else if (c === '"' || c === "'" || c === '`') quote = c;
    else if (c === '{') depth++;
    else if (c === '}' && --depth === 0) return html.slice(start, i + 1);
  }
  throw Error(`Unterminated ${name}`);
}
function context(names, extras = {}) {
  const ctx = vm.createContext({ ...extras, console, setTimeout, Date });
  for (const name of names) vm.runInContext(extract(name), ctx);
  return ctx;
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((r, j) => { resolve = r; reject = j; });
  return { promise, resolve, reject };
}
test('only latest Finance request commits both metrics and transactions', async () => {
  const jobs = [];
  let rendered = 0, alerts = 0;
  const ctx = context(['nbRefreshFinance'], {
    document: { getElementById: () => ({ value: 'all' }) },
    NB_FIN_PERIOD: 'Hoje', NB_FIN_STATUS: 'Todos', NB_FIN_PAGE: 1, NB_FIN_REQUEST_ID: 0,
    adminFetch: url => { const d = deferred(); jobs.push({ url, ...d }); return d.promise; },
    nbRenderFinanceView: () => rendered++, alert: () => alerts++,
  });
  const first = ctx.nbRefreshFinance();
  ctx.NB_FIN_PERIOD = 'Ontem';
  const second = ctx.nbRefreshFinance();
  for (const j of jobs.slice(2)) j.resolve({ ok: true, json: async () => ({ tag: 'new' }) });
  await second;
  for (const j of jobs.slice(0, 2)) j.resolve({ ok: true, json: async () => ({ tag: 'old' }) });
  await first;
  assert.equal(ctx.NB_FIN_METRICS.tag, 'new');
  assert.equal(ctx.NB_FIN_TRANSACTIONS.tag, 'new');
  assert.equal(rendered, 1);
  assert.equal(alerts, 0);
});
test('saved profiles sanitize legacy local cache and new snapshots', () => {
  const storage = new Map();
  const localStorage = { getItem: k => storage.get(k) || null, setItem: (k, v) => storage.set(k, v) };
  storage.set('SHARKBOT_SAVED_PROFILES', JSON.stringify([{ id: 'p', saved_at_ms: 3, snapshot: {
    publish: { bot_token: '111:fixture', gateway_secret: 'fixture-gw' }, account: { ai_api_key: 'fixture-ai' }, persona: { name: 'X' }
  } }]));
  const ctx = context(['sanitizeSavedProfiles', 'getMergedSavedProfiles'], { localStorage, STATE: { saved_profiles: [] } });
  const result = ctx.getMergedSavedProfiles();
  assert.equal(result[0].snapshot.persona.name, 'X');
  assert.ok(!JSON.stringify(result).includes('111:fixture'));
  assert.ok(!storage.get('SHARKBOT_SAVED_PROFILES').includes('fixture-ai'));
});
test('reset requests selected bot scope rather than first matching Telegram ID', async () => {
  let url;
  const ctx = context(['resetLeadHistory', 'nbLeadDeleteScope'], {
    STATE: { flow_bots: [{ id: 'a', bot_id: '111' }, { id: 'b', bot_id: '222' }], memory: { leads: [
      { telegram_id: '42', bot_id: 'a', origin_bot_numeric_id: '111', history: ['a'] },
      { telegram_id: '42', bot_id: 'b', origin_bot_numeric_id: '222', history: ['b'] },
    ] } },
    document: { getElementById: () => ({ value: 'b' }) },
    adminFetch: async u => { url = u; return { ok: false }; },
    saveLocalBackup: () => {}, renderMemoryLeads: () => {},
  });
  await ctx.resetLeadHistory('42');
  assert.match(url, /bot_id=b/);
  assert.equal(ctx.STATE.memory.leads[0].history[0], 'a');
  assert.equal(ctx.STATE.memory.leads[1].history.length, 0);
});
test('starts count by creation day, while paid events use BRT payment day', () => {
  assert.match(html, /const leadDateStr = l\.created_at \|\| l\.last_interaction/);
  assert.match(html, /NB_ANA_SELECTED_DATE \? nbIsBrtDay\(txDate, NB_ANA_SELECTED_DATE\)/);
  const ctx = context(['nbParseEventDate', 'nbIsBrtDay']);
  assert.equal(ctx.nbIsBrtDay('2026-10-04T01:30:00Z', '2026-10-03'), true);
  assert.equal(ctx.nbIsBrtDay('2026-10-04T01:30:00', '2026-10-03'), true);
  assert.equal(ctx.nbIsBrtDay('2026-10-04T03:30:00Z', '2026-10-03'), false);
});

test('calendar filters use BRT and do not count future or eighth day', () => {
  const ctx = context(['nbParseEventDate', 'nbMatchesDateFilter']);
  const today = new Date().toLocaleDateString('en-CA', { timeZone: 'America/Sao_Paulo' });
  const day = n => { const d = new Date(`${today}T12:00:00-03:00`); d.setUTCDate(d.getUTCDate() - n); return d.toISOString(); };
  assert.equal(ctx.nbMatchesDateFilter(day(6), '7 dias'), true);
  assert.equal(ctx.nbMatchesDateFilter(day(7), '7 dias'), false);
  assert.equal(ctx.nbMatchesDateFilter(day(-1), '7 dias'), false);
  assert.equal(ctx.nbMatchesDateFilter(day(29), '30d'), true);
  assert.equal(ctx.nbMatchesDateFilter(day(30), '30d'), false);
});
