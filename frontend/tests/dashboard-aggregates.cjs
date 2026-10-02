const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start) + start.length));
const els = new Map();
const el = id => {
  if (!els.has(id)) els.set(id, {value:'all',textContent:'',innerHTML:'',style:{},className:''});
  return els.get(id);
};
const metrics = {paid_count:2,paid_amount:30,generated_count:3,generated_amount:35,pending_count:1,pending_amount:5,
  by_day:[{day:'2026-09-20',paid_count:2,paid_amount:30}],
  by_hour:[{hour:10,paid_count:2,generated_count:3}],
  by_bot:[{origin_bot_id:'111',paid_count:2,paid_amount:30}],
  by_ticket:[{amount:10,paid_count:2}], recent_paid_days:[{day:'2026-09-20',paid_count:2}]};
const calls=[];
const context = {STATE:{flow_bots:[{id:'a',bot_id:'111',username:'@a',mode:'flow'}],memory:{leads:[]}},
  document:{getElementById:el},escapeHtml:String, NB_METRICS:null,NB_DASH_PERIOD:'Tudo',NB_ANA_SELECTED_DATE:'',
  NB_ANA_CAL_YEAR:2026,NB_ANA_CAL_MONTH:8,nbMatchesDateFilter:()=>true,nbDistinctTransactions:()=>[],
  adminFetch:async url=>{calls.push(url);return {ok:true,json:async()=>metrics}},nbUpdateHeaderRevenueWidget:()=>{},alert:()=>{}};
vm.createContext(context);
vm.runInContext(extract('async function nbRefreshMetrics()', 'function deleteLeadByRow('),context);
(async()=>{
  await context.nbRefreshMetrics();
  assert.equal(el('nb-dash-kpi-sales').textContent,'2');
  assert.equal(el('nb-dash-kpi-pending-cnt').textContent,'1 Pix aguardando pagamento');
  assert.equal(el('nb-ana-hour-best').textContent,'10h (2 vendas)');
  assert.match(el('nb-ana-top-bots').innerHTML,/@a/);
  assert.match(el('nb-ana-top-days').innerHTML,/2026-09-20/);
  assert.match(el('nb-ana-top-tickets').innerHTML,/2 plano\(s\)/);
  assert.match(el('nb-ana-top-flows').textContent + el('nb-ana-top-flows').innerHTML,/indisponível/i);
  assert.match(el('nb-ana-top-plans').textContent + el('nb-ana-top-plans').innerHTML,/indisponível/i);
  assert.match(el('nb-ana-rate-retention').textContent,/—|indisponível/i);
  assert.equal(el('nb-ana-cnt-buyers').textContent,'—');
  el('nb-ana-filter-bot').value='a';el('nb-ana-filter-flow').value='flow';
  await context.nbRefreshMetrics();
  assert.match(calls.at(-1),/bot_id=a.*flow=flow/);
  context.adminFetch=async()=>({ok:false});
  await context.nbRefreshMetrics();
  assert.equal(el('nb-dash-kpi-sales').textContent,'—', 'failed scope must not retain prior totals');
  assert.match(html,/id="nb-ana-filter-flow" onchange="nbRefreshMetrics\(\)"/);
  for (const id of ['gateway','source','type']) assert.match(html,new RegExp(`id="nb-ana-filter-${id}"[^>]*disabled`));
  console.log('Dashboard aggregate rendering checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
