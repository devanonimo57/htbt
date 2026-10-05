const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const mode = process.argv[3];
const output = process.argv[4];
const bot = {id:'flow-a',token:'111:test-only',plans:[],
  downsell_general:{enabled:true,sequences:[{id:'a',message:'A',trigger:'start',loop_enabled:true}]},
  downsell_pix:{enabled:true,sequences:[{id:'p',message:'P'}]}};
const elements = new Map();
const document = {getElementById(id) { if (!elements.has(id)) elements.set(id, {value:'7',innerHTML:'',className:'',textContent:'',checked:true}); return elements.get(id); }};
const context = {console,document,bot,NB_DOWNSELL_TRACK:'general',crypto:require('node:crypto').webcrypto,
  Date:class extends Date {static now() {return 123456;}}, Math,JSON,Array,String,Number,Boolean,parseInt,
  nbGetCurrentBot:()=>bot, escapeHtml:s=>String(s), nbRenderMediaThumb:()=>'', nbRenderDownsellMediaThumb:()=>'',
  NB_SELECTED_BOT_INDEX:0,nbDownsellDragIdx:null};
vm.createContext(context);
function load(name) {
  const regex = new RegExp('^    (?:async )?function '+name+'\\(', 'm');
  const start = html.search(regex);
  assert.notEqual(start,-1,'Missing shipped function '+name);
  const end = html.indexOf('\n    }',start);
  assert.notEqual(end,-1);
  vm.runInContext(html.slice(start,end+6),context,{filename:name});
}
for (const name of ['nbGetActiveDownsellObj','nbRenderDownsellTab','nbUpdateDownsellSeq','nbAddDownsellSequence',
                    'nbDuplicateDownsellSeq','nbMoveDownsellSeq','nbOnDownsellDragStart','nbOnDownsellDrop','nbToggleDownsellCollapse']) load(name);
if (html.includes('function nbNewDownsellId(')) load('nbNewDownsellId');
if (mode==='unified') {
  const first=context.nbGetActiveDownsellObj(bot);
  assert.deepEqual(Array.from(first.sequences,s=>s.id),['a','p'],'Legacy Pix must migrate into the unified list');
  assert.equal(first.sequences[1].trigger,'pix');
  context.NB_DOWNSELL_TRACK='pix';
  assert.equal(context.nbGetActiveDownsellObj(bot),first,'No hidden second editable track');
  context.NB_DOWNSELL_TRACK='general';
  context.nbRenderDownsellTab(bot);
  assert.equal(elements.get('nb-ds-header-title').textContent,'Downsell');
  context.nbMoveDownsellSeq(1,-1);
  assert.deepEqual(Array.from(first.sequences,s=>s.id),['p','a']);
  context.nbToggleDownsellCollapse(0);
  assert.equal(first.sequences[0]._collapsed,true);
  context.nbUpdateDownsellSeq(0,'target_audience','all');
  context.nbUpdateDownsellSeq(0,'loop_enabled',true);
  assert.equal(first.sequences[0].target_audience,'all');
  assert.equal(first.sequences[0].loop_enabled,true);
  context.nbOnDownsellDragStart({dataTransfer:{setData(){}}},0);
  context.nbOnDownsellDrop({preventDefault(){}},1);
  assert.deepEqual(Array.from(first.sequences,s=>s.id),['a','p']);
} else if (mode==='rest') {
  assert.match(html,/id="nb-ds-loop-rest"/,'A saved cycle rest control must exist');
  load('nbUpdateDownsellRest');
  context.nbUpdateDownsellRest('7');
  assert.equal(context.nbGetActiveDownsellObj(bot).loop_rest_minutes,7);
  context.nbRenderDownsellTab(bot);
  assert.equal(Number(elements.get('nb-ds-loop-rest').value),7);
  context.nbUpdateDownsellSeq(0,'target_audience','all');
  context.nbUpdateDownsellSeq(1,'loop_enabled',true);
  context.nbOnDownsellDragStart({dataTransfer:{setData(){}}},1);
  context.nbOnDownsellDrop({preventDefault(){}},0);
  context.nbToggleDownsellCollapse(0);
} else if (mode==='ids') {
  context.nbAddDownsellSequence(); context.nbAddDownsellSequence(); context.nbDuplicateDownsellSeq(0);
  const ids=context.nbGetActiveDownsellObj(bot).sequences.map(s=>s.id);
  assert.equal(new Set(ids).size,ids.length,'Rapid add/duplicate must not share an immutable card ID');
} else throw Error('Unknown mode');
fs.writeFileSync(output,JSON.stringify(bot,null,2));
console.log('PASS',mode);
