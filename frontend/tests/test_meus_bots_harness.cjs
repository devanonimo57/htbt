const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/);
assert(scriptMatch, 'script block not found');

const elements = new Map();
const getEl = id => {
  if (!elements.has(id)) {
    elements.set(id, {
      id, value: '', textContent: '', innerHTML: '',
      style: {}, classList: {
        add(...c){}, remove(...c){}, toggle(c, v){}, contains(c){ return false; }
      },
      checked: false, disabled: false, tagName: 'DIV',
      setAttribute: () => {}, getAttribute: () => null
    });
  }
  return elements.get(id);
};

const fakeBot = {
  id: 'bot_test_1',
  bot_id: '1234567890',
  username: '@test_flow_bot',
  mode: 'flow',
  active: true,
  sales_today: 3,
  revenue_today: 59.70,
  welcome: { message: 'Bem-vindo ao bot!', medias: [] },
  plans: [
    { id: 'plan_1', name: 'Plano VIP Mensal', price: 29.90, duration: 'Mensal (30 dias)', button_color: 'Verde' }
  ],
  downsell_general: {
    enabled: true,
    sequences: [
      { id: 'ds_1', delay_minutes: 10, trigger: 'start', target_audience: 'new', message: 'Oferta especial!', plan_ids: ['plan_1'] }
    ]
  },
  order_bumps: [
    { id: 'ob_1', title: 'Pack Bônus', price: 9.90, active: true, plan_ids: ['plan_1'] }
  ],
  payments: {
    generated_message: 'Pague via Pix abaixo',
    approved_message: 'Pagamento confirmado!'
  }
};

const context = {
  window: {
    addEventListener: () => {},
    removeEventListener: () => {}
  },
  document: {
    getElementById: getEl,
    querySelectorAll: () => [],
    addEventListener: () => {},
    removeEventListener: () => {}
  },
  saveFullState: async () => true,
  adminFetch: async () => ({ ok: true, json: async () => ({ status: 'ok' }) }),
  setTimeout: (fn) => fn(),
  console: console,
  assert: assert,
  fakeBot: fakeBot
};

vm.createContext(context);
vm.runInContext(scriptMatch[1], context);

vm.runInContext(`
  STATE.flow_bots = [fakeBot];
  NB_CURRENT_BOT_ID = 'bot_test_1';

  console.log('1. Testing nbRenderBotsList...');
  nbRenderBotsList();
  const gridHtml = document.getElementById('nb-bots-grid').innerHTML;
  assert.match(gridHtml, /@test_flow_bot/, 'Grid must render bot username');
  assert.match(gridHtml, /3 vendas/, 'Grid must render sales count');
  assert.match(gridHtml, /59,70/, 'Grid must render revenue today');
  assert.match(gridHtml, /BOTÕES/, 'Grid must mark mode as BOTÕES');

  console.log('2. Testing nbOpenBotDetail...');
  nbOpenBotDetail('bot_test_1');
  assert.equal(document.getElementById('nb-det-username').textContent, '@test_flow_bot');
  assert.equal(document.getElementById('nb-det-botid').textContent, 'ID 1234567890');

  console.log('3. Testing nbAddPlan...');
  const initialPlanCount = fakeBot.plans.length;
  nbAddPlan();
  assert.equal(fakeBot.plans.length, initialPlanCount + 1, 'Plan should be added');
  assert.equal(fakeBot.plans[1].name, 'Novo Plano VIP');

  console.log('4. Testing nbRemovePlan...');
  nbRemovePlan(1);
  assert.equal(fakeBot.plans.length, initialPlanCount, 'Plan should be removed');

  console.log('5. Testing nbRenderDownsellTab...');
  nbRenderDownsellTab(fakeBot);
  const dsHtml = document.getElementById('nb-ds-sequences-container').innerHTML;
  assert.match(dsHtml, /Sequência 1/);
  assert.match(dsHtml, /Oferta especial!/);

  console.log('6. Testing nbRenderOrderBumpTab...');
  nbRenderOrderBumpTab(fakeBot);
  const obHtml = document.getElementById('nb-ob-saved-list').innerHTML;
  assert.match(obHtml, /Pack Bônus/);
  assert.match(obHtml, /9,90/);

  console.log('7. Testing nbRenderPaymentsTab...');
  nbRenderPaymentsTab(fakeBot);
  assert.equal(document.getElementById('nb-pay-gen-msg').value, 'Pague via Pix abaixo');
  assert.equal(document.getElementById('nb-pay-app-msg').value, 'Pagamento confirmado!');

  console.log('8. Testing toggle bot status...');
  nbToggleBotFromCard('bot_test_1', false);
  assert.equal(fakeBot.active, false, 'Bot should be inactive');
  nbToggleBotFromCard('bot_test_1', true);
  assert.equal(fakeBot.active, true, 'Bot should be active');

  console.log('ALL 8 MEUS BOTS FLOW CONTROLLER TESTS PASSED 100% CLEAN!');
`, context);
