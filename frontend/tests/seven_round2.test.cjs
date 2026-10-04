'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'../..');
const report=process.env.HOTBOT_REPORT || require('node:os').tmpdir();
fs.mkdirSync(report,{recursive:true});
const html=fs.readFileSync(path.join(root,'frontend/public/index.html'),'utf8');
const block=(a,b)=>{const p=html.indexOf(a),q=html.indexOf(b,p+a.length);assert(p>=0&&q>p);return html.slice(p,q)};
const clone=x=>JSON.parse(JSON.stringify(x));
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{resolve,promise}};
function fixture(){return {account:{},publish:{},saved_profiles:[],remarketing_campaigns:[],memory:{leads:[]},flow_bots:[{id:'flow-a',bot_id:'111',token:'***configured***',active:true,mode:'flow',cache_channel_id:'-1001',welcome:{message:'ORIGINAL',medias:[]},plans:[],payments:{},downsell_general:{enabled:true,sequences:[{id:'general-1',message:'ORIGINAL GENERAL',medias:[]}]},downsell_pix:{enabled:true,sequences:[{id:'pix-1',message:'ORIGINAL PIX',medias:[]}]}},{id:'flow-b',bot_id:'222',token:'***configured***',active:true,mode:'flow',cache_channel_id:'-1002',welcome:{message:'BOT B',medias:[]},plans:[],payments:{}}]}}
function setup(initial=fixture()){
 const els=new Map();const el=id=>{if(!els.has(id))els.set(id,{value:'',checked:false,innerHTML:'',textContent:'',style:{},parentElement:{classList:{toggle(){}}},classList:{add(){},remove(){},toggle(){}}});return els.get(id)};
 const c=vm.createContext({STATE:clone(initial),document:{getElementById:el},NB_CURRENT_BOT_ID:'flow-a',NB_CURRENT_SUBTAB:'welcome',NB_DOWNSELL_TRACK:'general',NB_ACTIVE_CHAT_USER_ID:'7',NB_ACTIVE_CHAT_BOT_ID:'flow-a',NB_ACTIVE_CHAT_ORIGIN_BOT_ID:'111',BACKEND_ONLINE:true,getMergedSavedProfiles:x=>x||[],saveLocalBackup(){},syncStateToInputs(){},markUnsaved(){},markSaved(){},nbLoadBotIntoTabs(){},nbRenderPlansTab(){},nbRenderDownsellTab(){},nbUpdateWelcomeValidation(){},nbUpdateDetailHeaderStatus(){},nbSwitchMainView(){},nbSwitchBotTab(){},nbOpenLeadChatModal(){},nbRenderClientsView(){},loadInitialState:async()=>{},escapeHtml:x=>String(x),setTimeout(){},console,alert(){}});
 vm.runInContext(block('async function saveFullState()','async function loadInitialState()'),c);
 vm.runInContext(block('function nbGetCurrentBot()','async function nbCreateOperationGroup()'),c);
 vm.runInContext(block('function nbSyncActiveBotToPublish()','function nbBackToBotsList()'),c);
 vm.runInContext(block('function nbOpenBotDetail(','function nbUpdateDetailHeaderStatus('),c);
 vm.runInContext(block('function nbOnCacheChannelInput(','function nbInsertVar('),c);
 vm.runInContext(block('function nbOnWelcomeInput()','function nbUpdateWelcomeValidation()'),c);
 vm.runInContext(block('async function nbSaveCurrentBot()','document.addEventListener(\'click\''),c);
 vm.runInContext(block('async function nbTestDownsellSequence(','// ABA 4: ORDER BUMP'),c);
 vm.runInContext(block('async function nbSendLeadDirectMsg()','async function nbToggleLeadBlock()'),c);
 Object.assign(c,{NB_MAIN_VIEW:'clients',NB_AI_DETAIL_OPEN:false,NB_LEAVING_DETAIL:false,switchWorkspace(){},nbUpdateHeaderRevenueWidget(){},nbRenderBotsList(){}});
 vm.runInContext(block('async function nbSwitchMainView(view)','// TELA 6: BASE DE CLIENTES'),c);
 vm.runInContext(block('function nbCloseLeadChatModal()','async function nbSendLeadDirectMsg()'),c);
 el('chk-bot-live').checked=true;
 return {c,el};
}
async function crm(contract){
 const {c,el}=setup(contract?.initial);const gate=deferred();let request,post;
 el('nb-lead-chat-input').value='AUDIT CRM MESSAGE';
 c.adminFetch=async(url,opt)=>{if(url.includes('/leads/action')){request=JSON.parse(opt.body);await gate.promise;return{ok:true,json:async()=>contract?.action||{status:'ok',state:fixture()}}}post=JSON.parse(opt.body);return{ok:true,json:async()=>({state:post})}};
 const pending=c.nbSendLeadDirectMsg();
 // Close chat, navigate via the dock and open the bot's actual card handler.
 c.nbCloseLeadChatModal();assert.equal(await c.nbSwitchMainView('bots'),true);c.nbOpenBotDetail('flow-a');
 // The shipped welcome input handler creates the newer edit while CRM awaits.
 el('nb-welcome-msg').value='NEW UNSAVED WELCOME';c.nbOnWelcomeInput();
 assert.equal(c.STATE.flow_bots[0].welcome.message,'NEW UNSAVED WELCOME');
 gate.resolve();await pending;
 assert.equal(c.STATE.flow_bots[0].welcome.message,'NEW UNSAVED WELCOME');
 await c.saveFullState();
 return {request,post,lost_edit:'NEW UNSAVED WELCOME',after:c.STATE.flow_bots[0].welcome.message};
}
async function cacheSwitch(){
 const {c,el}=setup(),gate=deferred();let request,post;
 el('nb-bot-cache-channel').value='-1001';
 c.adminFetch=async(url,opt)=>{if(url==='/api/state'){post=JSON.parse(opt.body);await gate.promise;return{ok:true,json:async()=>({state:post})}}request=JSON.parse(opt.body);return{ok:true,json:async()=>({status:'error',message:'isolated capture'})}};
 // Leave detail through the real guarded navigation: the second save can
 // complete before the first in-flight save, making B reachable via its card.
 Object.assign(c,{NB_MAIN_VIEW:'detail',NB_AI_DETAIL_OPEN:false,NB_LEAVING_DETAIL:false,switchWorkspace(){},nbUpdateHeaderRevenueWidget(){},nbRenderBotsList(){}});
 vm.runInContext(block('async function nbSwitchMainView(view)','// TELA 6: BASE DE CLIENTES'),c);
 let saves=0;c.adminFetch=async(url,opt)=>{if(url==='/api/state'){const posted=JSON.parse(opt.body);if(++saves===1){post=posted;await gate.promise;}return{ok:true,json:async()=>({state:posted})}}request=JSON.parse(opt.body);return{ok:true,json:async()=>({status:'error',message:'isolated capture'})}};
 const pending=c.nbVerifyCacheChannel();
 assert.equal(await c.nbSwitchMainView('bots'),true);
 c.nbOpenBotDetail('flow-b');assert.equal(c.NB_MAIN_VIEW,'detail');
 gate.resolve();await pending;
 assert.equal(request,undefined,'A request must not use B after navigation');
 return{request,post,selected_before:'flow-a',selected_after:'flow-b',original_channel_b:'-1002'};
}
async function downsellFailure(all=false,kind='http'){
 const {c,el}=setup();const requests=[];let post;
 vm.runInContext(block('function nbSwitchDownsellTrack(','function nbRenderDownsellTab('),c);
 vm.runInContext(block('function nbUpdateDownsellSeq(','function nbToggleDownsellPlan('),c);
 c.nbUpdateDownsellSeq(0,'message','NEW UNSAVED DOWNSELL');
 c.adminFetch=async(url,opt)=>{if(url==='/api/state'){post=JSON.parse(opt.body);if(kind==='offline')throw new Error('offline');return{ok:false,status:500}}requests.push({url,body:JSON.parse(opt.body)});return{ok:true,json:async()=>({status:'success',sent_count:1,channel_id:'-1001',bot_username:'@fixture'})}};
 if(kind==='false') c.nbSaveCurrentBot=async()=>false;
 await (all?c.nbTestAllDownsellSequences():c.nbTestDownsellSequence(0));
 assert.equal(requests.length,0);assert.doesNotMatch(el('nb-ds-test-banner').innerHTML,/Sucesso/);assert.match(el('nb-ds-test-banner').innerHTML,/salvar/i);
 return{requests,post,save_http_status:500,banner:el('nb-ds-test-banner').innerHTML};
}
async function downsellTrack(){
 const {c}=setup(),gate=deferred();let request;
 c.adminFetch=async(url,opt)=>{if(url==='/api/state'){const posted=JSON.parse(opt.body);await gate.promise;return{ok:true,json:async()=>({state:posted})}}request=JSON.parse(opt.body);return{ok:true,json:async()=>({status:'success',channel_id:'-1001',bot_username:'@fixture'})}};
 vm.runInContext(block('function nbSwitchDownsellTrack(','function nbRenderDownsellTab('),c);
 const pending=c.nbTestDownsellSequence(0);c.nbSwitchDownsellTrack('pix');gate.resolve();await pending;
 assert.equal(request.track,'general');return{request,track_at_click:'general'};
}
async function cacheContract(contract){
 const {c,el}=setup(contract?.initial),gate=deferred();let request,post,firstPost,dirty=0;
 c.markUnsaved=()=>dirty++;el('nb-bot-cache-channel').value='-1001';
 c.adminFetch=async(url,opt)=>{if(url==='/api/state'){post=JSON.parse(opt.body);firstPost ||= clone(post);return{ok:true,json:async()=>({state:post})}}
  request=JSON.parse(opt.body);await gate.promise;return{ok:true,json:async()=>contract?.action||{status:'ok',title:'Fixture',state:clone(firstPost)}}};
 const pending=c.nbVerifyCacheChannel();for(let i=0;i<30&&!request;i++)await Promise.resolve();assert(request);
 el('nb-welcome-msg').value='NEW CACHE WELCOME';c.nbOnWelcomeInput();c.STATE.publish.gateway_secret='new-cache-secret';
 const media=c.STATE.flow_bots[0].welcome.medias[0];if(media)media.url='https://example.invalid/new-source';
 c.STATE.flow_bots[1].plans=[];
 gate.resolve();await pending;
 assert.equal(c.STATE.flow_bots[0].welcome.message,'NEW CACHE WELCOME');assert.equal(c.STATE.publish.gateway_secret,'new-cache-secret');
 if(media){assert.equal(c.STATE.flow_bots[0].welcome.medias[0].url,'https://example.invalid/new-source');assert(!c.STATE.flow_bots[0].welcome.medias[0].telegram_file_id)}
 assert.match(el('nb-cache-channel-status').textContent,/Conectado/);assert(dirty>0);
 await c.saveFullState();return{request,firstPost,post};
}
async function cacheControls(){
 for(const kind of ['http','offline']){
  const {c,el}=setup();let requests=0;el('nb-bot-cache-channel').value='-1001';
  c.adminFetch=async(url,opt)=>{if(url==='/api/state'){if(kind==='offline')throw new Error('offline');return{ok:false,status:500}}requests++;return{ok:true,json:async()=>({status:'ok'})}};
  await c.nbVerifyCacheChannel();assert.equal(requests,0);assert.match(el('nb-cache-channel-status').textContent,/salvar/);
 }
 for(const mutation of ['channel','channel-roundtrip','token','remove','replace','bot-roundtrip']){
  const {c,el}=setup(),gate=deferred();let requests=0,saves=0;el('nb-bot-cache-channel').value='-1001';
  c.adminFetch=async(url,opt)=>{if(url==='/api/state'){const posted=JSON.parse(opt.body);if(++saves===1)await gate.promise;return{ok:true,json:async()=>({state:posted})}}requests++;return{ok:true,json:async()=>({status:'error'})}};
  const pending=c.nbVerifyCacheChannel();
  if(mutation.startsWith('channel')){el('nb-bot-cache-channel').value='-1009';c.nbOnCacheChannelInput('-1009');if(mutation==='channel-roundtrip'){el('nb-bot-cache-channel').value='-1001';c.nbOnCacheChannelInput('-1001')}}
  if(mutation==='token')c.STATE.flow_bots[0].token='333:test-only';
  if(mutation==='remove')c.STATE.flow_bots.splice(0,1);
  if(mutation==='replace')c.STATE.flow_bots[0]={...clone(c.STATE.flow_bots[0]),bot_id:'333',token:'333:test-only'};
  if(mutation==='bot-roundtrip'){await c.nbSwitchMainView('bots');c.nbOpenBotDetail('flow-b');c.nbOpenBotDetail('flow-a')}
  gate.resolve();await pending;assert.equal(requests,0,mutation);
 }
 for(const mutation of ['bot','bot-roundtrip','channel','token','remove']){
  const {c,el}=setup(),gate=deferred();let request,firstPost;el('nb-bot-cache-channel').value='-1001';
  c.adminFetch=async(url,opt)=>{if(url==='/api/state'){const posted=JSON.parse(opt.body);firstPost ||= clone(posted);return{ok:true,json:async()=>({state:posted})}}request=JSON.parse(opt.body);await gate.promise;return{ok:true,json:async()=>({status:'ok',title:'OLD RESPONSE',state:firstPost})}};
  const pending=c.nbVerifyCacheChannel();for(let i=0;i<30&&!request;i++)await Promise.resolve();assert(request);
  if(mutation.startsWith('bot')){await c.nbSwitchMainView('bots');c.nbOpenBotDetail('flow-b');if(mutation==='bot-roundtrip')c.nbOpenBotDetail('flow-a')}
  if(mutation==='channel'){el('nb-bot-cache-channel').value='-1009';c.nbOnCacheChannelInput('-1009')}
  if(mutation==='token')c.STATE.flow_bots[0].token='333:test-only';if(mutation==='remove')c.STATE.flow_bots.splice(0,1);
  el('nb-cache-channel-status').textContent='NEW CONTEXT';gate.resolve();await pending;assert.equal(el('nb-cache-channel-status').textContent,'NEW CONTEXT',mutation);
 }
 await cacheContract();
}
async function previewControls(){
 const outputs=[];
 for(const all of [false,true]){
  for(const mutation of ['remove','reorder','replace','edit','token','bot','bot-roundtrip']){
   const {c}=setup(),gate=deferred(),requests=[];
   c.STATE.flow_bots[0].downsell_general.sequences.push({id:'general-2',message:'SECOND',medias:[]});
   vm.runInContext(block('function nbSwitchDownsellTrack(','function nbRenderDownsellTab('),c);
   let saves=0;c.adminFetch=async(url,opt)=>{if(url==='/api/state'){const posted=JSON.parse(opt.body);if(++saves===1)await gate.promise;return{ok:true,json:async()=>({state:posted})}}requests.push(JSON.parse(opt.body));return{ok:true,json:async()=>({status:'error'})}};
   const pending=all?c.nbTestAllDownsellSequences():c.nbTestDownsellSequence(0);
   const b=c.STATE.flow_bots[0],seqs=b.downsell_general.sequences;
   if(mutation==='remove')seqs.splice(0,1);
   if(mutation==='reorder')seqs.reverse();
   if(mutation==='replace')seqs[0]={id:'replacement',message:'OTHER',medias:[]};
   if(mutation==='edit')seqs[0].message='NEW EDIT';
   if(mutation==='token')b.token='333:test-only';
   if(mutation.startsWith('bot')){assert.equal(await c.nbSwitchMainView('bots'),true);c.nbOpenBotDetail('flow-b');if(mutation==='bot-roundtrip')c.nbOpenBotDetail('flow-a');}
   gate.resolve();await pending;assert.equal(requests.length,0,`${all}/${mutation}`);
  }
  const {c,el}=setup(),sendGate=deferred(),refreshGate=deferred();let sendReached=false,getReached=false,post,request;
  let dirty=0;c.markUnsaved=()=>dirty++;
  c.STATE.flow_bots[0].plans=[{id:'remove-plan',name:'Delete later'}];
  c.STATE.flow_bots[0].downsell_general.sequences[0].message='NEW SAVED DOWNSELL';
  c.adminFetch=async(url,opt)=>{
   if(url==='/api/state'&&opt){post=JSON.parse(opt.body);return{ok:true,json:async()=>({state:post})}}
   if(url==='/api/state'){getReached=true;await refreshGate.promise;return{ok:true,json:async()=>clone(post)}}
   request=JSON.parse(opt.body);sendReached=true;await sendGate.promise;return{ok:true,json:async()=>({status:'success',sent_count:all?1:1,channel_id:'-1001'})};
  };
  const pending=all?c.nbTestAllDownsellSequences():c.nbTestDownsellSequence(0);
  for(let i=0;i<30&&!sendReached;i++)await Promise.resolve();assert(sendReached);
  el('nb-welcome-msg').value='EDIT DURING SEND';c.nbOnWelcomeInput();
  c.STATE.publish.gateway_secret='new-local-secret';c.STATE.flow_bots[0].plans=[];
  sendGate.resolve();for(let i=0;i<30&&!getReached;i++)await Promise.resolve();assert(getReached);
  c.STATE.flow_bots[1].welcome.message='EDIT DURING GET';refreshGate.resolve();await pending;
  assert.equal(c.STATE.flow_bots[0].welcome.message,'EDIT DURING SEND');assert.equal(c.STATE.flow_bots[1].welcome.message,'EDIT DURING GET');
  assert.equal(c.STATE.publish.gateway_secret,'new-local-secret');assert.equal(c.STATE.flow_bots[0].plans.length,0);assert(dirty>0);assert.match(el('nb-ds-test-banner').innerHTML,/Sucesso/);
  outputs.push({all,request,post});
 }
 return outputs;
}
(async()=>{
 const mode=process.argv[2]||'crm';
 if(mode==='crm-request'){
  const {c,el}=setup(JSON.parse(fs.readFileSync(process.argv[3],'utf8')));let request;
  el('nb-lead-chat-input').value='AUDIT CRM MESSAGE';
  c.adminFetch=async(url,opt)=>{request=JSON.parse(opt.body);return{ok:true,json:async()=>({status:'ok'})}};
  await c.nbSendLeadDirectMsg();console.log(JSON.stringify(request));return;
 }
 if(mode==='crm-response'){
  const contract=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));const output=await crm(contract);
  fs.writeFileSync(path.join(report,'crm-node-result.json'),JSON.stringify(output,null,2));console.log('CRM contract passed');return;
 }
 if(mode==='downsell-failure'){const outputs=[];for(const all of [false,true])for(const kind of ['http','offline','false'])outputs.push(await downsellFailure(all,kind));fs.writeFileSync(path.join(report,'downsell-failure-result.json'),JSON.stringify(outputs));console.log('Downsell failed-save contract passed');return;}
 if(mode==='downsell-track'){const output=await downsellTrack();fs.writeFileSync(path.join(report,'downsell-track-result.json'),JSON.stringify(output));console.log('Downsell track contract passed');return;}
 if(mode==='preview-controls'){fs.writeFileSync(path.join(report,'preview-controls-result.json'),JSON.stringify(await previewControls()));console.log('Preview controls passed');return;}
 if(mode==='cache-switch'){const output=await cacheSwitch();fs.writeFileSync(path.join(report,'cache-switch-result.json'),JSON.stringify(output));console.log('Cache switch passed');return;}
 if(mode==='cache-controls'){await cacheControls();console.log('Cache guards passed');return;}
 if(mode==='cache-contract'){const contract=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));fs.writeFileSync(path.join(report,'cache-contract-result.json'),JSON.stringify(await cacheContract(contract)));console.log('Cache contract passed');return;}
 assert.equal(mode,'crm');await crm();
 let scripts=0;for(const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)){if(m[1].trim()){new vm.Script(m[1]);scripts++}}
 console.log(JSON.stringify({passed:mode,parsed_inline_scripts:scripts}));
})().catch(e=>{console.error(e);process.exitCode=1});
