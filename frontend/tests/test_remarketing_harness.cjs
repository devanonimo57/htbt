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

const context = {
  window: { addEventListener: () => {}, removeEventListener: () => {} },
  document: { getElementById: getEl, querySelectorAll: () => [], addEventListener: () => {}, removeEventListener: () => {} },
  saveFullState: async () => true,
  saveLocalBackup: () => {},
  getMergedSavedProfiles: () => [],
  adminFetch: async () => ({ ok: true, json: async () => ({ status: 'ok' }) }),
  setTimeout: fn => fn(),
  console: console,
  assert: assert
};

vm.createContext(context);
vm.runInContext(scriptMatch[1], context);

vm.runInContext(`
  STATE.remarketing_campaigns = [];
  STATE.flow_bots = [{ id: 'bot_1', username: '@vip_bot' }];

  console.log('1. Testing Remarketing Empty State...');
  nbRenderRemarketingView();
  assert.equal(document.getElementById('nb-rmk-kpi-total').textContent, '0');
  assert.match(document.getElementById('nb-rmk-campaigns-container').innerHTML, /Pronto para começar/);

  console.log('2. Testing Create Campaign in Modal...');
  nbOpenRemarketingModal();
  document.getElementById('nb-rmk-inp-name').value = 'Recuperação VIP 50%';
  document.getElementById('nb-rmk-inp-bot').value = 'bot_1';
  document.getElementById('nb-rmk-inp-segment').value = 'Pendentes';
  document.getElementById('nb-rmk-inp-status').value = 'Ativa';
  document.getElementById('nb-rmk-inp-discount').value = 50;
  document.getElementById('nb-rmk-inp-msg').value = 'Sua vaga com 50% de desconto!';
  document.getElementById('nb-rmk-inp-include-plans').checked = true;

  (async () => {
    await nbSaveRemarketingCampaign(false);
    assert.equal(STATE.remarketing_campaigns.length, 1);
    assert.equal(STATE.remarketing_campaigns[0].name, 'Recuperação VIP 50%');
    assert.equal(STATE.remarketing_campaigns[0].discount_pct, 50);

    console.log('3. Testing Remarketing Render with Campaign Cards...');
    nbRenderRemarketingView();
    assert.equal(document.getElementById('nb-rmk-kpi-total').textContent, '1');
    assert.equal(document.getElementById('nb-rmk-kpi-active').textContent, '1');
    const container = document.getElementById('nb-rmk-campaigns-container').innerHTML;
    assert.match(container, /Recuperação VIP 50%/);
    assert.match(container, /-50% OFF/);
    assert.match(container, /@vip_bot/);

    console.log('4. Testing Remarketing Filter and Search...');
    document.getElementById('nb-rmk-search').value = 'Recuperação';
    nbRenderRemarketingView();
    assert.match(document.getElementById('nb-rmk-campaigns-container').innerHTML, /Recuperação VIP 50%/);

    document.getElementById('nb-rmk-search').value = 'NaoExiste';
    nbRenderRemarketingView();
    assert.match(document.getElementById('nb-rmk-campaigns-container').innerHTML, /Nenhuma campanha encontrada/);
    document.getElementById('nb-rmk-search').value = '';

    console.log('5. Testing Delete Campaign...');
    const campId = STATE.remarketing_campaigns[0].id;
    await nbDeleteRemarketingCampaign(campId);
    assert.equal(STATE.remarketing_campaigns.length, 0);
    assert.match(document.getElementById('nb-rmk-campaigns-container').innerHTML, /Pronto para começar/);

    console.log('ALL 5 REMARKETING HARNESS TESTS PASSED 100% CLEAN!');
  })().catch(e => { console.error(e); process.exitCode = 1; });
`, context);
