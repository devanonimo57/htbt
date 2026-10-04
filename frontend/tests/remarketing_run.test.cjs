const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../public/index.html'),'utf8');
const source=html.slice(html.indexOf('function nbMergeStateResponse('), html.indexOf('async function loadInitialState(')) + html.slice(html.indexOf('async function nbSendRemarketingCampaign('),html.indexOf('async function nbDeleteRemarketingCampaign('));
function setup(responses, campaign={id:'camp',name:'C',run_id:'old-id',deliveries:{'111:41':'uncertain'}}){
 const requests=[],els=new Map();let backups=[];
 const c=vm.createContext({STATE:{remarketing_campaigns:[campaign]},document:{getElementById(id){if(!els.has(id))els.set(id,{textContent:'',classList:{remove(){}}});return els.get(id)}},confirm:()=>true,crypto:{randomUUID:()=> 'proposed-id'},saveLocalBackup(){backups.push(JSON.parse(JSON.stringify(c.STATE)))},getMergedSavedProfiles:()=>[],nbRenderRemarketingView(){},adminFetch:async(url,opt)=>{requests.push({url,payload:JSON.parse(opt.body)});const response=responses.shift();if(response instanceof Error)throw response;return response},console});
 vm.runInContext(source,c);return {c,requests,backups};
}
async function rejected(status){
 const {c,requests}=setup([{ok:false,status},{ok:false,status:409},{ok:false,status:409}]);
 await c.nbSendRemarketingCampaign('camp',true);
 assert.equal(c.STATE.remarketing_campaigns[0].run_id,'old-id','rejection must preserve confirmed run');
 assert.ok(!c.STATE.remarketing_campaigns[0].pending_new_run);
 await c.nbReconcileRemarketing('camp','111:41','sent');
 await c.nbSendRemarketingCampaign('camp');
 assert.equal(requests[1].payload.run_id,'old-id');assert.equal(requests[2].payload.run_id,'old-id');assert.equal(requests[2].payload.start_new_run,false);
 return requests;
}
(async()=>{
 if(process.argv[2]==='--asgi-rejection'){console.log(JSON.stringify(await rejected(Number(process.argv[3]))));return;}
 for(const status of [400,401,403,404,409,422])await rejected(status);
 for(const failure of [new Error('response lost'),{ok:false,status:500}]){
  const {c,requests,backups}=setup([failure,new Error('still unavailable'),new Error('still unavailable')]);
  await c.nbSendRemarketingCampaign('camp',true);
  assert.equal(c.STATE.remarketing_campaigns[0].run_id,'old-id');
  await c.nbSendRemarketingCampaign('camp');
  await c.nbSendRemarketingCampaign('camp',true);
  assert.equal(requests[1].payload.run_id,'proposed-id');assert.equal(requests[1].payload.start_new_run,true);
  assert.equal(requests[2].payload.run_id,'proposed-id','new action cannot discard unresolved proposal');
  assert.ok(backups.some(s=>s.remarketing_campaigns[0].pending_run_id==='proposed-id'));
 }
 const accepted={ok:true,json:async()=>({state:{remarketing_campaigns:[{id:'camp',run_id:'proposed-id'}]},sent_count:1,has_more:false})};
 const {c,requests}=setup([accepted,{ok:false,status:409}]);await c.nbSendRemarketingCampaign('camp',true);await c.nbSendRemarketingCampaign('camp');
 assert.equal(c.STATE.remarketing_campaigns[0].run_id,'proposed-id');assert.equal(requests[1].payload.start_new_run,false);
 console.log('remarketing run: rejected, ambiguous, accepted and idempotent retry checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
