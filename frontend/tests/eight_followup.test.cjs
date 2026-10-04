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
async function campaignEdits() {
  const {c, el} = setup(), d = deferred(); let dirty = 0, saved = 0;
  c.markUnsaved = () => dirty++; c.markSaved = () => saved++;
  c.STATE.flow_bots = [{id:'a', token:'111:fake-old', welcome:{message:'before', medias:[{id:'m1'},{id:'m2'}]}}, {id:'b'}];
  c.STATE.remarketing_campaigns = [{id:'camp', run_id:'old', message:'offer'}, {id:'deleted'}];
  const server = JSON.parse(JSON.stringify(c.STATE)); server.flow_bots[0].sales_today = 2;
  server.remarketing_campaigns[0] = {...server.remarketing_campaigns[0], run_id:'new-run', sent_count:1, status:'Finalizada'};
  c.adminFetch = async () => { await d.promise; return {ok:true, json:async()=>({has_more:false, sent_count:1, total_targets:1, state:server})}; };
  vm.runInContext(block('async function nbSendRemarketingCampaign(', 'async function nbReconcileRemarketing('), c);
  const pending = c.nbSendRemarketingCampaign('camp', true);
  c.STATE.flow_bots[0].welcome.message = 'typed while sending'; c.STATE.flow_bots[0].token = '111:fake-fresh';
  c.STATE.flow_bots[0].welcome.medias.shift(); c.STATE.flow_bots.splice(1,1);
  c.STATE.remarketing_campaigns[0].message = 'new edited offer'; c.STATE.remarketing_campaigns.splice(1,1);
  d.resolve(); await pending;
  assert.equal(c.STATE.flow_bots[0].welcome.message, 'typed while sending');
  assert.equal(c.STATE.flow_bots[0].token, '111:fake-fresh'); assert.equal(c.STATE.flow_bots.length, 1);
  assert.equal(c.STATE.flow_bots[0].welcome.medias.length, 1);
  assert.equal(c.STATE.remarketing_campaigns.length, 1); assert.equal(c.STATE.remarketing_campaigns[0].message, 'new edited offer');
  assert.equal(c.STATE.remarketing_campaigns[0].run_id, 'new-run'); assert.equal(c.STATE.remarketing_campaigns[0].sent_count, 1);
  assert.ok(!c.STATE.remarketing_campaigns[0].pending_run_id); assert.ok(dirty > 0); assert.equal(saved, 0);
  // Removing the sending campaign while pending must never recreate it.
  const second = setup(), hold = deferred(); second.c.STATE.remarketing_campaigns = [{id:'camp', run_id:'r'}];
  const response = JSON.parse(JSON.stringify(second.c.STATE));
  second.c.adminFetch = async()=>{ await hold.promise; return {ok:true,json:async()=>({state:response, has_more:true})}; };
  vm.runInContext(block('async function nbSendRemarketingCampaign(', 'async function nbReconcileRemarketing('),second.c);
  const send = second.c.nbSendRemarketingCampaign('camp'); second.c.STATE.remarketing_campaigns = []; hold.resolve(); await send;
  assert.equal(second.c.STATE.remarketing_campaigns.length, 0);
  const reconciliation = setup(), held = deferred();
  reconciliation.c.STATE.flow_bots = [{id:'a',welcome:{message:'old'}}];
  reconciliation.c.STATE.remarketing_campaigns = [{id:'camp',run_id:'run'}];
  const oldState = JSON.parse(JSON.stringify(reconciliation.c.STATE)); oldState.remarketing_campaigns[0].sent_count = 1;
  reconciliation.c.adminFetch = async()=>{ await held.promise; return {ok:true,json:async()=>({state:oldState})}; };
  vm.runInContext(block('async function nbReconcileRemarketing(', 'async function nbDeleteRemarketingCampaign('),reconciliation.c);
  const reconcile = reconciliation.c.nbReconcileRemarketing('camp','111:7','sent');
  reconciliation.c.STATE.flow_bots[0].welcome.message = 'reconcile newer edit'; held.resolve(); await reconcile;
  assert.equal(reconciliation.c.STATE.flow_bots[0].welcome.message, 'reconcile newer edit');
  assert.equal(reconciliation.c.STATE.remarketing_campaigns[0].sent_count, 1);
  console.log('PASS campaign merge: edits, deletions, secrets, dirty + accepted run/reconcile');
}
(async () => { await modalContexts(); await campaignEdits(); })().catch(e => { console.error(e); process.exitCode = 1; });
