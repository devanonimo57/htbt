const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../public/index.html'),'utf8');
function fixture(){const els=new Map(),jobs=[];const el=id=>{if(!els.has(id))els.set(id,{value:'',textContent:'',innerHTML:'',classList:{toggle(){}}});return els.get(id)};
 const c=vm.createContext({document:{getElementById:el},STATE:{flow_bots:[{id:'a',username:'@a'},{id:'b',username:'@b'}],tx_records:{'legacy-a':{bot_id:'a',amount:999,status:'paid'}}},NB_FIN_PERIOD:'Total',NB_FIN_STATUS:'Todos',NB_FIN_PAGE:1,NB_FIN_METRICS:{paid_count:1,paid_amount:99,pending_amount:0},NB_FIN_TRANSACTIONS:{items:[{tx_id:'a-sale',bot_id:'a',amount:99,status:'paid'}],total:1},nbMatchesDateFilter:()=>true,escapeHtml:String,alert(){},console:{warn(){}},adminFetch(url){let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j});jobs.push({url,resolve,reject});return promise}});
 vm.runInContext(html.slice(html.indexOf('function nbDistinctTransactions('),html.indexOf('async function nbClearBotTestSales(')),c);return {c,el,jobs};}
function unavailable(el,label){for(const id of ['nb-fin-rev-value','nb-fin-paid-count','nb-fin-ticket-value','nb-fin-pending-value'])assert.equal(el(id).textContent,'—',id);assert.match(el('nb-fin-tx-container').innerHTML,label);assert.doesNotMatch(el('nb-fin-tx-container').innerHTML,/a-sale|legacy-a|NENHUMA TRANSAÇÃO/);}
test('latest failed bot refresh invalidates metrics and rows, then recovers',async()=>{
 for(const failure of ['metrics','transactions','network','json']){
  const {c,el,jobs}=fixture();el('nb-fin-bot-select').value='a';c.nbRenderFinanceView();el('nb-fin-bot-select').value='b';const refresh=c.nbRefreshFinance();
  unavailable(el,/Carregando/);
  jobs[0].resolve({ok:failure!=='metrics',json:async()=>{if(failure==='json')throw Error('bad json');return {paid_count:2,paid_amount:20,pending_amount:3}}});
  if(failure==='network')jobs[1].reject(Error('offline'));else jobs[1].resolve({ok:failure!=='transactions',json:async()=>({items:[],total:0})});
  await refresh;c.nbRenderFinanceView();unavailable(el,/indisponível/i);
  const retry=c.nbRefreshFinance();jobs[2].resolve({ok:true,json:async()=>({paid_count:0,paid_amount:0,pending_amount:0})});jobs[3].resolve({ok:true,json:async()=>({items:[],total:0})});await retry;
  assert.equal(el('nb-fin-rev-value').textContent,'R$ 0,00');assert.match(el('nb-fin-tx-container').innerHTML,/NENHUMA TRANSAÇÃO/);
 }
});
test('stale failure cannot invalidate latest successful metrics and list',async()=>{
 const {c,el,jobs}=fixture();el('nb-fin-bot-select').value='a';const old=c.nbRefreshFinance();el('nb-fin-bot-select').value='b';const latest=c.nbRefreshFinance();
 jobs[2].resolve({ok:true,json:async()=>({paid_count:1,paid_amount:25,pending_amount:0})});jobs[3].resolve({ok:true,json:async()=>({items:[{tx_id:'b-sale',bot_id:'b',amount:25,status:'paid'}],total:1})});await latest;
 jobs[0].reject(Error('old offline'));jobs[1].resolve({ok:false});await old;
 assert.equal(el('nb-fin-rev-value').textContent,'R$ 25,00');assert.match(el('nb-fin-tx-container').innerHTML,/b-sale/);
});
