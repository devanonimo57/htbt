'use strict';
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'../..');
const REPORT=process.env.HOTBOT_REPORT;
const library=fs.readFileSync(ROOT+'/frontend/tests/seven_round2.test.cjs','utf8');
const host=vm.createContext({require,__dirname:ROOT+'/frontend/tests',process,console});
vm.runInContext(library.slice(0,library.indexOf('(async()=>{'))+'\nglobalThis.H={setup,block,clone,deferred};',host);
const {setup,block,clone,deferred}=host.H;
async function run(){
 const [mode,kind,input]=process.argv.slice(2);
 const contract=JSON.parse(fs.readFileSync(input,'utf8'));
 const {c,el}=setup(contract.initial);
 vm.runInContext(block('async function nbToggleLeadBlock()','// TELA 7: REMARKETING'),c);
 vm.runInContext(block('function nbLeadDeleteScope(','function renderLearningKpis()'),c);
 Object.assign(c,{confirm:()=>true,renderMemoryLeads(){},renderLearningKpis(){}});
 let request,posted,dirty=0,opened=0;
 c.markUnsaved=()=>dirty++;c.nbOpenLeadChatModal=()=>opened++;
 const gate=deferred();
 c.adminFetch=async(url,opt)=>{
  if(url==='/api/state'){posted=JSON.parse(opt.body);return{ok:true,json:async()=>({state:posted})}}
  request={url,method:opt.method,body:opt.body?JSON.parse(opt.body):undefined};
  if(mode==='request')return{ok:true,json:async()=>({status:'ok'})};
  await gate.promise;return{ok:!(contract.http_status>=400),json:async()=>contract.action};
 };
 const invoke=()=>kind==='block'?c.nbToggleLeadBlock():kind==='delete'?c.deleteLead('7',null,'flow-a'):c.nbUpdateAccessStatus(contract.tx_id,kind==='remove'?'Removido':'Ativo');
 const pending=invoke();
 if(mode==='request'){await pending;console.log(JSON.stringify(request));return;}
 if(mode==='response'||mode==='failure'){
  c.nbCloseLeadChatModal();assert.equal(await c.nbSwitchMainView('bots'),true);c.nbOpenBotDetail('flow-a');
  el('nb-welcome-msg').value='NEW EDIT DURING ACTION';c.nbOnWelcomeInput();
  assert.equal(c.STATE.flow_bots[0].welcome.message,'NEW EDIT DURING ACTION');
  c.STATE.publish.gateway_secret='NEW ACTION SECRET';
  c.STATE.flow_bots[0].welcome.medias=[];c.STATE.flow_bots[1].plans=[];
  c.NB_ACTIVE_CHAT_USER_ID='8';c.NB_ACTIVE_CHAT_BOT_ID='flow-b';c.NB_ACTIVE_CHAT_ORIGIN_BOT_ID='222';
  el('nb-lead-chat-input').value='NEW CHAT DRAFT';
  gate.resolve();await pending;
  const after=c.STATE.flow_bots[0].welcome.message;
  assert.equal(after,'NEW EDIT DURING ACTION');
  assert.equal(c.STATE.publish.gateway_secret,'NEW ACTION SECRET');
  assert.equal(c.STATE.flow_bots[0].welcome.medias.length,0);
  assert.equal(c.STATE.flow_bots[1].plans.length,0);
  assert.equal(el('nb-lead-chat-input').value,'NEW CHAT DRAFT');
  assert.equal(opened,0,'must not reopen another chat');
  assert(dirty>0);
  if(kind==='block')assert.equal(request.body.origin_bot_numeric_id,'111');
  if(kind==='access')assert.equal(request.body.tx_id,contract.tx_id);
  await c.saveFullState();
  const output={kind,request,typed:'NEW EDIT DURING ACTION',after,post:posted};
  fs.writeFileSync(path.join(REPORT,kind+'-observed.json'),JSON.stringify(output,null,2));
  console.log(JSON.stringify({kind,after,expected_correct:true}));return;
 }
 if(mode==='removal-response'){
  gate.resolve();await pending;
  const output={request,banner:el('nb-acc-feedback-banner').textContent};
  if(contract.http_status>=400){
   assert.doesNotMatch(output.banner,/✓|Status do acesso atualizado/);
   assert.match(output.banner,/remover|remoção|Telegram|falha/i);
  }else assert.match(output.banner,/Status do acesso atualizado.*Removido/);
  fs.writeFileSync(path.join(REPORT,'removal-ui-observed.json'),JSON.stringify(output,null,2));console.log(JSON.stringify(output));return;
 }
 throw Error('Unknown mode');
}
run().catch(e=>{console.error(e);process.exitCode=1});
