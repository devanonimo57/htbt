const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../public/index.html'),'utf8');
function fixture(){const input={value:'  message for A  '},button={disabled:false},opened=[];let resolve,payload;
const c=vm.createContext({NB_ACTIVE_CHAT_USER_ID:'42',NB_ACTIVE_CHAT_BOT_ID:'a',NB_ACTIVE_CHAT_ORIGIN_BOT_ID:'111',document:{getElementById:id=>id==='nb-lead-chat-input'?input:button},adminFetch:(_url,opt)=>{payload=JSON.parse(opt.body);return new Promise(r=>resolve=r)},nbOpenLeadChatModal:(...args)=>opened.push(args),nbRenderClientsView(){},alert(){}});
vm.runInContext(html.slice(html.indexOf('async function nbSendLeadDirectMsg()'),html.indexOf('async function nbToggleLeadBlock()')),c);return {c,input,button,opened,resolve:res=>resolve(res),payload:()=>payload};}
for(const field of ['NB_ACTIVE_CHAT_USER_ID','NB_ACTIVE_CHAT_BOT_ID','NB_ACTIVE_CHAT_ORIGIN_BOT_ID'])test(`response from old chat preserves draft when ${field} changes`,async()=>{
 const f=fixture();const inflight=f.c.nbSendLeadDirectMsg();f.c[field]='other';f.input.value='new chat draft';f.resolve({ok:true,json:async()=>({status:'ok'})});await inflight;
 assert.deepEqual(f.payload(),{user_id:'42',bot_id:'a',origin_bot_numeric_id:'111',action:'send_message',message:'message for A'});
 assert.equal(f.input.value,'new chat draft');assert.equal(f.opened.length,0);assert.equal(f.button.disabled,false);
});
test('edited draft in same chat survives old send success',async()=>{const f=fixture(),p=f.c.nbSendLeadDirectMsg();f.input.value='next message';f.resolve({ok:true,json:async()=>({status:'ok'})});await p;assert.equal(f.input.value,'next message');});
test('unchanged original draft clears only on successful send',async()=>{for(const ok of [true,false]){const f=fixture(),p=f.c.nbSendLeadDirectMsg();f.resolve({ok,json:async()=>({status:ok?'ok':'error'})});await p;assert.equal(f.input.value,ok?'':'  message for A  ');}});
