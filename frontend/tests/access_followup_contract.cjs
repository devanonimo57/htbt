'use strict';
// Exercise the shipped handler, reusing the existing DOM/VM fixture.
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const [mode,input,output]=process.argv.slice(2);
if(!input){console.log('PASS access_followup_contract: skipped standalone invocation (called via python harness)');process.exit(0);}
const lib=fs.readFileSync(path.join(__dirname,'seven_round2.test.cjs'),'utf8');
const host=vm.createContext({require,__dirname,process,console});
vm.runInContext(lib.slice(0,lib.indexOf('(async()=>{'))+'\nglobalThis.H={setup,block,deferred};',host);
const {setup,block,deferred}=host.H;
(async()=>{
 const contract=JSON.parse(fs.readFileSync(input,'utf8'));
 const {c,el}=setup(contract.initial); c.window=c;
 vm.runInContext(block('async function nbResendAccess(','async function nbUpdateAccessStatus('),c);
 const gate=deferred();let request,post,backup,dirty=0,opened=[];
 c.markUnsaved=()=>dirty++;c.saveLocalBackup=()=>{backup=JSON.parse(JSON.stringify(c.STATE));};
 c.nbOpenLeadChatModal=(...args)=>opened.push(args);
 el('nb-lead-chat-input').value='original draft';
 c.adminFetch=async(url,opt)=>{
  if(url==='/api/state'){post=JSON.parse(opt.body);return{ok:true,json:async()=>({state:post})};}
  request={url,method:opt.method,body:JSON.parse(opt.body)};
  if(mode==='request')return{ok:true,json:async()=>({status:'ok',sent_count:0})};
  await gate.promise;
  if(mode==='error')throw new Error('offline resend');
  return{ok:contract.http_status===200,json:async()=>contract.action};
 };
 const pending=c.nbResendAccess(contract.tx_id);
 if(mode==='request'){await pending;fs.writeFileSync(output,JSON.stringify(request,null,2));return;}
 const edited=mode==='late'||mode==='error';
 if(edited){
  c.nbCloseLeadChatModal();assert.equal(await c.nbSwitchMainView('bots'),true);c.nbOpenBotDetail('flow-a');
  el('nb-welcome-msg').value='NEW WELCOME WHILE RESEND';c.nbOnWelcomeInput();
  c.STATE.publish.gateway_secret='NEW LOCAL TEST SECRET';
  c.STATE.flow_bots[0].welcome.medias=[];c.STATE.flow_bots[1].plans=[];
  c.NB_ACTIVE_CHAT_USER_ID='9';c.NB_ACTIVE_CHAT_BOT_ID='flow-b';c.NB_ACTIVE_CHAT_ORIGIN_BOT_ID='222';
  el('nb-lead-chat-input').value='new peer draft';
  dirty=0;
 }
 gate.resolve();await pending;
 if(edited){
  assert.equal(c.STATE.flow_bots[0].welcome.message,'NEW WELCOME WHILE RESEND');
  assert.equal(c.STATE.publish.gateway_secret,'NEW LOCAL TEST SECRET');
  assert.equal(c.STATE.flow_bots[0].welcome.medias.length,0);assert.equal(c.STATE.flow_bots[1].plans.length,0);
  assert.equal(el('nb-lead-chat-input').value,'new peer draft');
  assert.equal(c.NB_ACTIVE_CHAT_USER_ID,'9');assert.equal(c.NB_ACTIVE_CHAT_BOT_ID,'flow-b');assert.equal(c.NB_ACTIVE_CHAT_ORIGIN_BOT_ID,'222');
  assert.equal(opened.length,0);
  if(mode==='late'){assert(dirty>0,'Late edits must still be marked unsaved');assert.equal(backup.flow_bots[0].welcome.message,'NEW WELCOME WHILE RESEND');}
  else assert.match(el('nb-acc-feedback-banner').textContent,/offline resend/);
 }else{
  assert.equal(dirty,0,'Ordinary resend must not warn about new edits');
  assert.equal(c.STATE.flow_bots[0].welcome.message,contract.initial.flow_bots[0].welcome.message);
  assert.equal(el('nb-lead-chat-input').value,'original draft');
 }
 if(mode!=='error'){
  assert.equal(JSON.stringify(c.STATE.memory.leads),JSON.stringify(contract.action.state.memory.leads));
  assert(c.STATE.memory.leads[0].history.some(item=>item.content==='RUNTIME WHILE RESEND'));
  assert(!('tx_records' in contract.action.state),'Real panel API omits Pix rows');
  assert.match(el('nb-acc-feedback-banner').textContent,/sucesso/);
  await c.saveFullState();assert(post);
 }
 fs.writeFileSync(output,JSON.stringify({request,post,dirty,banner:el('nb-acc-feedback-banner').textContent},null,2));
 console.log('PASS shipped nbResendAccess '+mode);
})().catch(e=>{console.error(e);process.exitCode=1});
