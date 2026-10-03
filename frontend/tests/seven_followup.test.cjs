const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../public/index.html'),'utf8');
const block=(a,b)=>html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)+a.length));
function setup(){
 const els=new Map(),el=id=>{if(!els.has(id))els.set(id,{value:'',checked:false,innerHTML:'',textContent:'',classList:{add(){},remove(){}}});return els.get(id)};
 let saved=0,dirty=0,closed=0;
 const c=vm.createContext({STATE:{account:{},publish:{},saved_profiles:[],remarketing_campaigns:[{id:'camp',name:'Original',message:'OLD',bot_id:'flow'}],flow_bots:[]},document:{getElementById:el},NB_EDITING_RMK_ID:'camp',NB_CURRENT_BOT_ID:null,BACKEND_ONLINE:true,getMergedSavedProfiles:x=>x||[],saveLocalBackup(){},syncStateToInputs(){},markSaved(){saved++},markUnsaved(){dirty++},nbCloseRemarketingModal(){closed++},nbRenderRemarketingView(){},confirm:()=>true,crypto:{randomUUID:()=> 'new-run'},console});
 vm.runInContext(block('async function saveFullState()', 'async function loadInitialState()'),c);
 return {c,el,counts:()=>({saved,dirty,closed})};
}
async function failedCampaignSave(){
 for(const failure of [{ok:false,status:500},new Error('offline')]){
  const {c,el,counts}=setup(),calls=[];
  el('nb-rmk-inp-name').value='Edited';el('nb-rmk-inp-msg').value='NEW';
  c.adminFetch=async(url,opt)=>{calls.push(url);if(url==='/api/state'){if(failure instanceof Error)throw failure;return failure}return {ok:true,json:async()=>({has_more:false})}};
  vm.runInContext(block('async function nbSaveRemarketingCampaign(', 'async function nbReconcileRemarketing('),c);
  await c.nbSaveRemarketingCampaign(true);
  assert.deepEqual(calls,['/api/state'],'failed persistence must abort Telegram dispatch');
  assert.equal(c.STATE.remarketing_campaigns[0].message,'NEW');assert.equal(counts().closed,0);
 }
}
(async()=>{await failedCampaignSave();console.log('seven followup frontend checks passed')})().catch(e=>{console.error(e);process.exitCode=1});
