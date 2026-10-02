const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
const extract = (start, end) => {
  const a = html.indexOf(start), b = html.indexOf(end, a + start.length);
  assert(a >= 0 && b > a, `missing block ${start}`);
  return html.slice(a, b);
};
assert(!extract('function nbUpdateHeaderRevenueWidget()', 'let NB_LEAVING_DETAIL').includes('Math.max(botsRev'), 'header revenue must use paid records');
assert(!/nb-ana-kpi-visits|VIRARAM STARTS|Math\.round\(startsCount \* 1\.18\)/.test(html), 'fabricated visits must be removed');
assert(/onclick="nbRefreshMetrics\(\)"[\s\S]*?id="nb-dash-period-pills"/.test(html), 'refresh button immediately precedes period pills');
assert(!/onclick="clearAllLeads\(\)"|Apagar registro/.test(html), 'bulk CRM and individual finance deletion unavailable');
const els = new Map();
const el = id => {
  if (!els.has(id)) els.set(id, { textContent:'', innerHTML:'', value:'all', style:{}, classList:{toggle(){}}, });
  return els.get(id);
};
const today = new Date().toISOString().slice(0,10);
const old = '2024-01-02T12:00:00';
const current = `${today}T12:00:00`;
const tx_records = {
  first: {bot_id:'a',status:'paid',amount:10,paid_at:current},
  second: {bot_id:'a',status:'pending',amount:20,created_at:current},
  third: {bot_id:'b',status:'paid',amount:30,paid_at:old},
  fourth: {bot_id:'b',status:'pending',amount:40,created_at:old},
  duplicate: {tx_id:'first',bot_id:'a',status:'paid',amount:10,paid_at:current},
};
const context = {STATE:{flow_bots:[{id:'a',username:'@a',sales_today:999,revenue_today:999},{id:'b',username:'@b'}],tx_records,memory:{leads:[]}},
  document:{getElementById:el}, NB_FIN_PERIOD:'Hoje',NB_FIN_STATUS:'Todos',
  NB_FIN_TRANSACTIONS:null,NB_FIN_METRICS:null,NB_FIN_PAGE:1,NB_METRICS:null,
  escapeHtml:String, NB_DASH_PERIOD:'Hoje', NB_ANA_SELECTED_DATE:'',NB_ANA_CAL_YEAR:2026,NB_ANA_CAL_MONTH:9,
  nbMatchesDateFilter:(date,period)=>period==='Total'||(date||'').startsWith(today),
};
vm.createContext(context);
vm.runInContext(extract('function nbDistinctTransactions(', 'async function nbDeleteTransaction('),context);
context.nbRenderFinanceView();
assert.equal(el('nb-fin-rev-value').textContent,'R$ 10,00');
assert.equal(el('nb-fin-paid-count').textContent,'1');
assert.equal(el('nb-fin-pending-value').textContent,'R$ 20,00');
el('nb-fin-bot-select').value='b';
context.NB_FIN_PERIOD='Total'; context.nbRenderFinanceView();
assert.equal(el('nb-fin-rev-value').textContent,'R$ 30,00');
assert.equal(el('nb-fin-paid-count').textContent,'1');
assert.equal(el('nb-fin-pending-value').textContent,'R$ 40,00');
const crm = extract('async function deleteLead(', 'async function nbRefreshLeadsState(');
assert.match(crm,/bot_id/);
assert.match(extract('function nbLeadDeleteScope(', 'async function deleteLead('), /'all'/);
const render = extract('function nbRenderClientsView()', 'function nbOpenLeadChatModal(');
assert.match(render,/deleteLeadByRow/);
assert.match(render,/nbPermanentlyDeleteLeadByRow/);
// Exercise dashboard KPI calculation against records, not inflated bot counters.
vm.runInContext(extract('async function nbRefreshMetrics()', 'function deleteLeadByRow('),context);
context.nbRenderDashboardView();
assert.equal(el('nb-dash-kpi-sales').textContent,'1');
assert.equal(el('nb-dash-kpi-pending-cnt').textContent,'1 Pix aguardando pagamento');
assert.equal(el('nb-dash-hero-value').textContent,'10,00');
assert.equal(el('nb-ana-kpi-rev-gen').textContent,'R$ 30,00');

// Exercise explicit all-bot and selected-bot deletion requests.
const calls = [];
Object.assign(context, {
  confirm:()=>true, adminFetch:async (url,options)=>{calls.push({url,options}); return {ok:true,json:async()=>({state:context.STATE})}},
  saveLocalBackup:()=>{},renderMemoryLeads:()=>{},renderLearningKpis:()=>{}, nbRenderClientsView:()=>{},alert:()=>{},
});
vm.runInContext(extract('function nbLeadDeleteScope(', 'async function nbRefreshLeadsState('),context);
(async()=>{
  await context.deleteLead('42');
  assert.equal(calls[0].url, '/api/leads/42?bot_id=all');
  el('nb-leads-filter-bot').value='a';
  await context.deleteLead('42');
  assert.equal(calls[1].url, '/api/leads/42?bot_id=a');
  calls.length=0;
  context.adminFetch=async url=>{
    calls.push(url);
    return {ok:true,json:async()=>({paid_count:1,paid_amount:9,pending_count:0,pending_amount:0,generated_count:1,generated_amount:9,all_time_paid_amount:9})};
  };
  const renderOriginal = context.nbRenderDashboardView;
  context.nbRenderDashboardView=()=>{};
  context.nbUpdateHeaderRevenueWidget=()=>{};
  await context.nbRefreshMetrics();
  assert.deepEqual(calls,['/api/metrics?period=Hoje&bot_id=all']);
  assert.equal(context.NB_METRICS.paid_amount,9);
  assert.equal(context.STATE.flow_bots[0].id,'a');
  context.NB_FIN_PERIOD='Total'; context.NB_FIN_STATUS='Todos'; context.NB_FIN_PAGE=1;
  context.adminFetch=async url=>{
    calls.push(url);
    return {ok:true,json:async()=>url.startsWith('/api/transactions')
      ? {total:26,page:1,page_size:25,items:[{tx_id:'page-one',bot_id:'a',status:'paid',amount:11}]}
      : {paid_count:26,paid_amount:286,pending_amount:0}};
  };
  el('nb-fin-bot-select').value='a';
  await context.nbRefreshFinance();
  assert(calls.some(url=>url.includes('/api/transactions?period=Total&bot_id=a&status=all&page=1&page_size=25')));
  assert.equal(el('nb-fin-paid-count').textContent,'26');
  assert.match(el('nb-fin-tx-container').innerHTML,/page-one/);
  assert.match(el('nb-fin-tx-container').innerHTML,/Próxima/);
  context.nbRenderDashboardView=renderOriginal;
  console.log('Panel/Financeiro/CRM checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
