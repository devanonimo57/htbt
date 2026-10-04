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
  c.STATE.remarketing_campaigns = [{id:'camp', run_id:'old', bot_id:'a', message:'offer'}, {id:'deleted'}];
  const server = JSON.parse(JSON.stringify(c.STATE)); server.flow_bots[0].sales_today = 2;
  server.remarketing_campaigns[0] = {...server.remarketing_campaigns[0], run_id:'new-run', sent_count:1, status:'Finalizada'};
  c.adminFetch = async () => { await d.promise; return {ok:true, json:async()=>({has_more:false, sent_count:1, total_targets:1, state:server})}; };
  vm.runInContext(block('async function nbSendRemarketingCampaign(', 'async function nbReconcileRemarketing('), c);
  const pending = c.nbSendRemarketingCampaign('camp', true);
  c.STATE.flow_bots[0].welcome.message = 'typed while sending'; c.STATE.flow_bots[0].token = '111:fake-fresh';
  c.STATE.flow_bots[0].welcome.medias.shift(); c.STATE.flow_bots.splice(1,1);
  c.STATE.remarketing_campaigns[0].message = 'new edited offer'; c.STATE.remarketing_campaigns[0].bot_id = 'edited-target'; c.STATE.remarketing_campaigns.splice(1,1);
  d.resolve(); await pending;
  assert.equal(c.STATE.flow_bots[0].welcome.message, 'typed while sending');
  assert.equal(c.STATE.flow_bots[0].token, '111:fake-fresh'); assert.equal(c.STATE.flow_bots.length, 1);
  assert.equal(c.STATE.flow_bots[0].welcome.medias.length, 1);
  assert.equal(c.STATE.remarketing_campaigns.length, 1); assert.equal(c.STATE.remarketing_campaigns[0].message, 'new edited offer');
  assert.equal(c.STATE.remarketing_campaigns[0].bot_id, 'edited-target'); assert.equal(c.STATE.remarketing_campaigns[0].run_id, 'new-run'); assert.equal(c.STATE.remarketing_campaigns[0].sent_count, 1);
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
async function cacheSaveRaces() {
  for (const change of ['save-first','removed','removed-bot','replaced-media','replaced-bot','caption-edit','save-mask']) {
    const {c} = setup(), d = deferred();
    const bot = {id:'a',bot_id:'111',token:'111:fake-old',welcome:{medias:[{id:'m',type:'Fotos',url:'data:image/png;base64,AAAA',caption:'old'}]}};
    c.STATE.flow_bots = [bot]; c.nbGetCurrentBot = () => null;
    c.adminFetch = async (url,opt) => url.includes('cache-media') ? (await d.promise, {json:async()=>({status:'ok',telegram_file_id:'cached-A',telegram_send_as:'photo'})}) : {ok:true,json:async()=>{const state=JSON.parse(opt.body); if(change==='save-mask') state.flow_bots[0].token='***configured***'; return {state};}};
    vm.runInContext(block('async function nbAutoWarmMediaItem(', 'function nbHandleMediaUpload('), c);
    const pending = c.nbAutoWarmMediaItem(bot,bot.welcome.medias[0],'welcome');
    await c.saveFullState();
    if(change==='removed') c.STATE.flow_bots[0].welcome.medias=[];
    if(change==='removed-bot') c.STATE.flow_bots=[];
    if(change==='replaced-media') c.STATE.flow_bots[0].welcome.medias[0].url='data:image/png;base64,BBBB';
    if(change==='replaced-bot') c.STATE.flow_bots[0] = {id:'a',bot_id:'222',token:'222:fake-new',welcome:{medias:[{id:'m',type:'Fotos',url:'data:image/png;base64,AAAA'}]}};
    if(change==='caption-edit') c.STATE.flow_bots[0].welcome.medias[0].caption='typed caption';
    d.resolve(); await pending;
    if(change==='removed-bot') { assert.equal(c.STATE.flow_bots.length,0); continue; }
    const live = c.STATE.flow_bots[0].welcome.medias[0];
    if(change==='removed') assert.equal(live,undefined);
    else if(change.startsWith('replaced')) assert.equal(live.telegram_file_id,undefined);
    else { assert.equal(live.telegram_file_id,'cached-A',change); assert.ok(!live._caching,change); if(change==='caption-edit') assert.equal(live.caption,'typed caption'); }
  }
  // Older save response must not erase completed cache or hide its failed save.
  const {c,el} = setup(), cacheGate=deferred(), saveGate=deferred(); let saves=0;
  const b={id:'a',bot_id:'111',token:'***configured***',welcome:{medias:[{id:'m',url:'data:image/png;base64,AAAA'}]}};
  c.STATE.flow_bots=[b]; c.nbGetCurrentBot=()=>null;
  c.adminFetch=async(url,opt)=>{if(url.includes('cache-media')){await cacheGate.promise;return{json:async()=>({status:'ok',telegram_file_id:'completed'})}}; const posted=JSON.parse(opt.body); if(++saves===1){await saveGate.promise;return{ok:true,json:async()=>({state:posted})}} return{ok:false,status:500};};
  vm.runInContext(block('async function nbAutoWarmMediaItem(', 'function nbHandleMediaUpload('),c);
  const warm=c.nbAutoWarmMediaItem(b,b.welcome.medias[0],'welcome'), save=c.saveFullState();
  cacheGate.resolve();await warm;saveGate.resolve();await save;
  const media=c.STATE.flow_bots[0].welcome.medias[0];assert.equal(media.telegram_file_id,'completed');assert.ok(!media._caching);
  assert.match(el('nb-save-status').textContent,/não salvas|Não foi possível/);
  for (const replacement of ['media','bot']) {
    const {c} = setup(), hold = deferred();
    c.STATE.flow_bots=[{id:'a',bot_id:'111',token:'***configured***',welcome:{medias:[{id:'m',type:'Fotos',url:'old-source'}]}}];
    c.adminFetch=async(url,opt)=>{const state=JSON.parse(opt.body);state.flow_bots[0].welcome.medias[0].telegram_file_id='old-origin-file';await hold.promise;return{ok:true,json:async()=>({state})};};
    const save=c.saveFullState();
    if(replacement==='media') c.STATE.flow_bots[0].welcome.medias[0].url='new-source';
    else {c.STATE.flow_bots[0].bot_id='222';c.STATE.flow_bots[0].token='222:fake-replacement';}
    hold.resolve();await save;
    assert.equal(c.STATE.flow_bots[0].welcome.medias[0].telegram_file_id,undefined,`stale runtime for replaced ${replacement}`);
  }
  // A second upload for the same item wins, independent of response order.
  const sup=setup(), first=deferred(), second=deferred();
  sup.c.STATE.flow_bots=[{id:'a',bot_id:'111',token:'***configured***',welcome:{medias:[{id:'m',url:'src'}]}}];sup.c.nbGetCurrentBot=()=>null;
  let calls=0;sup.c.saveFullState=async()=>true;
  sup.c.adminFetch=async()=>{const n=++calls;await (n===1?first:second).promise;return{json:async()=>({status:'ok',telegram_file_id:n===1?'old-upload':'new-upload'})};};
  vm.runInContext(block('async function nbAutoWarmMediaItem(', 'function nbHandleMediaUpload('),sup.c);
  const sb=sup.c.STATE.flow_bots[0], sm=sb.welcome.medias[0];const old=sup.c.nbAutoWarmMediaItem(sb,sm,'welcome'), fresh=sup.c.nbAutoWarmMediaItem(sb,sm,'welcome');
  second.resolve();await fresh;first.resolve();await old;assert.equal(sm.telegram_file_id,'new-upload');assert.ok(!sm._caching);
  console.log('PASS cache/save: live IDs, removed/replaced context, captions, superseding, redaction + failed persistence');
}
(async () => { await modalContexts(); await campaignEdits(); await cacheSaveRaces(); })().catch(e => { console.error(e); process.exitCode = 1; });
