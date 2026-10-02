const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
function extract(start, end) {
  const i = html.indexOf(start);
  const j = html.indexOf(end, i + start.length);
  assert(i >= 0 && j > i, `missing source block: ${start}`);
  return html.slice(i, j);
}
const classifySrc = extract('function nbTransactionMatchesLead(', 'function nbMatchesDateFilter(');
const renderSrc = extract('function nbRenderClientsView()', 'function nbOpenLeadChatModal(');
const modalSrc = extract('function nbOpenLeadChatModal(', 'function nbCloseLeadChatModal(');
const helpers = vm.createContext({});
vm.runInContext(classifySrc, helpers);
const classify = helpers.nbClassifyLeadStatus;
const alpha = {telegram_id:'42', bot_id:'alpha', origin_bot_numeric_id:'111', first_name:'Ana', history:[]};
const beta = {telegram_id:'42', bot_id:'beta', origin_bot_numeric_id:'222', first_name:'Bia', history:[]};
const tx = {tx_id:'pix-beta',user_id:42,bot_id:'beta',origin_bot_numeric_id:'222',status:'paid',offer_name:'Plano Beta'};
assert.equal(classify(alpha,[tx]), 'Novo', 'payment on beta must not mark alpha paid');
assert.equal(classify(beta,[tx]), 'Pago');
const previousBot = {telegram_id:'42', bot_id:'beta', origin_bot_numeric_id:'333', first_name:'Antigo', history:[]};
assert.equal(classify(previousBot,[tx]), 'Novo', 'a reused configuration ID cannot transfer a sale');

const elements = {
  'nb-leads-table-body': {innerHTML:''},
  'nb-acc-table-body': {innerHTML:''},
  'nb-lead-chat-title': {textContent:''},
  'nb-lead-chat-sub': {textContent:''},
  'nb-lead-chat-block-btn': {classList:{toggle(){}},textContent:''},
  'nb-lead-chat-send-btn': {parentElement:{classList:{toggle(){}}}},
  'nb-lead-chat-history': {innerHTML:'',scrollTop:0,scrollHeight:0},
  'nb-modal-lead-chat': {classList:{remove(){}}},
};
const ctx = {
  STATE:{flow_bots:[{id:'alpha',username:'@alpha'},{id:'beta',username:'@beta'}],
         memory:{leads:[alpha,beta],deleted_leads:[]},tx_records:{'pix-beta':tx},business:{offers:[]}},
  NB_LEADS_PERIOD:'Tudo', window:{},
  document:{getElementById:id=>elements[id] || null},
  escapeHtml:s=>String(s ?? ''),
  nbClassifyLeadStatus:classify,
  nbTransactionMatchesLead:helpers.nbTransactionMatchesLead,
  nbMatchesDateFilter:()=>true,
  NB_ACTIVE_CHAT_USER_ID:'',
  NB_ACTIVE_CHAT_BOT_ID:'',
};
vm.createContext(ctx);
vm.runInContext(`${renderSrc}\n${modalSrc}`,ctx);
ctx.nbRenderClientsView();
const table = elements['nb-leads-table-body'].innerHTML;
const alphaRow = table.match(/<tr[^>]*>[\s\S]*?Ana[\s\S]*?<\/tr>/)?.[0] || '';
const betaRow = table.match(/<tr[^>]*>[\s\S]*?Bia[\s\S]*?<\/tr>/)?.[0] || '';
assert(alphaRow.includes('Ana'));
assert(!alphaRow.includes('Plano Beta'), 'alpha row must not show beta purchase');
assert(betaRow.includes('Plano Beta'), 'beta row should show its purchase');
const accessTable = elements['nb-acc-table-body'].innerHTML;
assert(accessTable.includes('Bia'), 'beta purchase should show beta lead name');
assert(!accessTable.includes('Ana'), 'beta purchase must not show alpha lead name');
assert.match(table, /nbOpenLeadChatModalByRow\(1, false\)/);
ctx.nbOpenLeadChatModalByRow(1, false);
assert(elements['nb-lead-chat-title'].textContent.includes('Bia'), 'chat must open beta lead');
assert.equal(ctx.NB_ACTIVE_CHAT_BOT_ID, 'beta');
assert.equal(ctx.NB_ACTIVE_CHAT_ORIGIN_BOT_ID, '222');
assert.match(html, /origin_bot_numeric_id:\s*NB_ACTIVE_CHAT_ORIGIN_BOT_ID/, 'admin action must send immutable origin');
assert.match(html, /bot_id:\s*NB_ACTIVE_CHAT_BOT_ID/, 'admin action must send bot identity');
console.log('CRM bot isolation frontend checks passed');
