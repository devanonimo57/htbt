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
async function pendingEditsSurviveSave(){
 const {c,el,counts}=setup();let release;const waiting=new Promise(r=>release=r);
 c.STATE.flow_bots=[{id:'a',token:'111:old',welcome:{message:'before',medias:[{id:'m',url:'old'}]}},{id:'b',welcome:{message:'delete me'}}];
 c.adminFetch=async(url,opt)=>{const posted=JSON.parse(opt.body);await waiting;posted.flow_bots[0].token='***configured***';posted.flow_bots[0].welcome.medias[0].telegram_file_id='cached';posted.memory={leads:[]};return {ok:true,json:async()=>({state:posted})}};
 c.nbGetCurrentBot=()=>c.STATE.flow_bots.find(b=>b.id==='a');c.nbUpdateWelcomeValidation=()=>{};
 vm.runInContext(block('function nbOnWelcomeInput()', 'function nbUpdateWelcomeValidation()'),c);
 const pending=c.saveFullState();el('nb-welcome-msg').value='new edit';c.nbOnWelcomeInput();c.STATE.flow_bots[0].token='111:new-secret';c.STATE.flow_bots.splice(1,1);release();await pending;
 assert.equal(c.STATE.flow_bots[0].welcome.message,'new edit','late save response must preserve unsaved input');
 assert.equal(c.STATE.flow_bots[0].token,'111:new-secret');assert.equal(c.STATE.flow_bots.length,1);
 assert.equal(c.STATE.flow_bots[0].welcome.medias[0].telegram_file_id,'cached');
 assert.equal(counts().saved,0);assert.ok(counts().dirty>0);
 const fresh=setup();fresh.c.adminFetch=async(u,o)=>({ok:true,json:async()=>({state:JSON.parse(o.body)})});
 assert.equal(await fresh.c.saveFullState(),true);assert.equal(fresh.counts().saved,1);
}
function capturedUpload(){
 for(const deleted of [false,true]){
  const {c}=setup(),a={id:'a',token:'111:a',welcome:{medias:[]}},b={id:'b',token:'222:b',welcome:{medias:[]},payments:{approved_medias:[]}};let current=a,reader;const warmed=[];
  c.STATE.flow_bots=[a,b];
  Object.assign(c,{NB_MEDIA_TARGET:{section:'welcome',seqIdx:0},NB_DOWNSELL_TRACK:'general',NB_OB_TEMP_MEDIAS:[],FileReader:class{constructor(){reader=this;this.result='data:image/png;base64,AAAA'}readAsDataURL(){}},nbGetCurrentBot:()=>current,nbLoadBotIntoTabs(){},nbAutoWarmMediaItem:(bot,item,section)=>warmed.push({bot:bot.id,item:item.name,section})});
  vm.runInContext(block('function nbHandleMediaUpload(', 'function nbRenderMediaChips('),c);
  c.nbHandleMediaUpload({target:{files:[{name:'a.png',type:'image/png'}]}});current=b;c.NB_MEDIA_TARGET={section:'pay_app',seqIdx:9};if(deleted)c.STATE.flow_bots.splice(0,1);reader.onload();
  assert.equal(b.welcome.medias.length,0,'must not attach A upload to currently selected B');
  assert.equal(a.welcome.medias.length,deleted?0:1);assert.equal(warmed.length,deleted?0:1);if(!deleted)assert.equal(warmed[0].bot,'a');
 }
}
(async()=>{await failedCampaignSave();await pendingEditsSurviveSave();capturedUpload();console.log('seven followup frontend checks passed')})().catch(e=>{console.error(e);process.exitCode=1});
