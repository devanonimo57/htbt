const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
const block = (a, b) => { const p = html.indexOf(a), q = html.indexOf(b, p + a.length); assert(p >= 0 && q > p); return html.slice(p, q); };
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };
function setup() {
  const els = new Map();
  const el = id => { if (!els.has(id)) els.set(id, {value:'', checked:false, textContent:'', innerHTML:'', classList:{add(){}, remove(){}, toggle(){}}}); return els.get(id); };
  const c = vm.createContext({STATE:{account:{}, publish:{}, flow_bots:[], saved_profiles:[], remarketing_campaigns:[]}, document:{getElementById:el}, NB_CURRENT_BOT_ID:null, NB_EDITING_BOT_ID:null, BACKEND_ONLINE:true, getMergedSavedProfiles:x=>x||[], saveLocalBackup(){}, syncStateToInputs(){}, markSaved(){}, markUnsaved(){}, nbLoadBotIntoTabs(){}, nbRenderBotsList(){}, nbSyncActiveBotToPublish(){}, nbRenderRemarketingView(){}, nbSelectModalBotMode(){}, confirm:()=>true, console, crypto:{randomUUID:()=> 'new-run'}});
  vm.runInContext(block('async function saveFullState()', 'async function loadInitialState()'), c);
  return {c, el};
}
async function modalContexts() {
  for (const change of ['switch', 'reopen', 'roundtrip', 'edit', 'edit-roundtrip', 'removed', 'replaced']) {
    const {c, el} = setup(), d = deferred();
    c.STATE.flow_bots = [{id:'a', bot_id:'111', token:'111:fake-old'}, {id:'b', bot_id:'222', token:'222:fake-old'}];
    vm.runInContext(block('function nbOpenAddBotModal()', 'async function nbDeleteEditingBot()'), c);
    c.nbOpenEditBotModal('a'); el('nb-inp-bot-token').value = '111:fake-new';
    let posts = 0;
    c.adminFetch = async (url, opt) => url.includes('verify-token') ? (await d.promise, {json:async()=>({status:'ok', bot_id:'111', username:'@A', first_name:'A'})}) : (posts++, {ok:true, json:async()=>({state:JSON.parse(opt.body)})});
    const pending = c.nbSubmitBotModal();
    if (change === 'switch') c.nbOpenEditBotModal('b');
    if (change === 'reopen') { c.nbCloseBotModal(); c.nbOpenEditBotModal('a'); el('nb-inp-bot-token').value = '111:fake-new'; }
    if (change === 'roundtrip') { c.nbOpenEditBotModal('b'); c.nbOpenEditBotModal('a'); el('nb-inp-bot-token').value = '111:fake-new'; }
    if (change.startsWith('edit')) { el('nb-inp-bot-token').value = '111:fake-edit'; if (c.nbInvalidateBotModal) c.nbInvalidateBotModal(); if (change === 'edit-roundtrip') { el('nb-inp-bot-token').value = '111:fake-new'; if (c.nbInvalidateBotModal) c.nbInvalidateBotModal(); } }
    if (change === 'removed') c.STATE.flow_bots.shift();
    if (change === 'replaced') c.STATE.flow_bots[0] = {id:'a', bot_id:'333', token:'333:fake-replacement'};
    const before = JSON.stringify(c.STATE.flow_bots);
    d.resolve(); await pending;
    assert.equal(JSON.stringify(c.STATE.flow_bots), before, `stale verification must not mutate ${change}`);
    assert.equal(posts, 0, `stale verification must not save ${change}`);
  }
  const {c, el} = setup(); c.STATE.flow_bots = [{id:'a', bot_id:'111', token:'111:fake-old'}];
  vm.runInContext(block('function nbOpenAddBotModal()', 'async function nbDeleteEditingBot()'), c);
  c.nbOpenEditBotModal('a'); el('nb-inp-bot-token').value = '111:fake-new';
  c.adminFetch = async (url,opt) => url.includes('verify-token') ? {json:async()=>({status:'ok', bot_id:'111', username:'@A', first_name:'A'})} : {ok:true, json:async()=>({state:JSON.parse(opt.body)})};
  await c.nbSubmitBotModal(); assert.equal(c.STATE.flow_bots[0].token, '111:fake-new');
  assert.match(html, /id="nb-inp-bot-token"[^>]*oninput="nbInvalidateBotModal\(\)"/);
  console.log('PASS token context: 7 stale cases + current submission');
}
(async () => { await modalContexts(); })().catch(e => { console.error(e); process.exitCode = 1; });
