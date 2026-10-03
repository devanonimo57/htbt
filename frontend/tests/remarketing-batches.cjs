const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
const source = html.slice(html.indexOf('async function nbSendRemarketingCampaign('), html.indexOf('async function nbDeleteRemarketingCampaign('));
assert(source.length > 0);
const banner = {classList:{remove(){},add(){}}, textContent:''};
const responses = [{sent_count:10,total_targets:12,has_more:true,state:{remarketing_campaigns:[{id:'c'}]}},
                   {sent_count:2,total_targets:12,has_more:false,state:{remarketing_campaigns:[{id:'c'}]}}];
let calls = 0;
const ctx = {STATE:{remarketing_campaigns:[{id:'c',name:'C',segment:'all'}]},
  document:{getElementById:()=>banner},
  adminFetch:async()=>{calls++;return {ok:true,json:async()=>responses.shift()};},
  getMergedSavedProfiles:()=>[],saveLocalBackup(){},nbRenderRemarketingView(){},
  setTimeout(){}, Number, JSON};
vm.createContext(ctx);
vm.runInContext(source,ctx);
const renderSource = html.slice(html.indexOf('function nbRenderRemarketingView()'), html.indexOf('function nbOpenRemarketingModal('));
const container = {innerHTML:''};
const renderCtx = {STATE:{remarketing_campaigns:[{id:'c',name:'C',status:'Revisão necessária',deliveries:{'111:41':'sending','111:42':'uncertain'}}],flow_bots:[]},
  document:{getElementById:id=>id === 'nb-rmk-campaigns-container' ? container : null},
  escapeHtml:s=>String(s),Number,Math,Object,encodeURIComponent};
vm.createContext(renderCtx);
vm.runInContext(renderSource,renderCtx);
renderCtx.nbRenderRemarketingView();
assert.match(container.innerHTML,/111:41/);
assert.match(container.innerHTML,/111:42/);
assert.match(container.innerHTML,/Confirmar não entregue/);
(async()=>{
  await ctx.nbSendRemarketingCampaign('c');
  assert.equal(calls,2,'must await and continue batches');
  assert.match(banner.textContent,/12/);
  console.log('remarketing batches passed');
})().catch(e=>{console.error(e);process.exitCode=1});
