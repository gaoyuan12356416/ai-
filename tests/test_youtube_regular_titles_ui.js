'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const listeners = {};
let current, request, copied;
const channels = [{id:'1',name:'日本語チャンネル',language:'ja',language_label:'日语',eligible:true},
  {id:'2',name:'繁體頻道',language:'zh-tw',language_label:'繁体中文',eligible:true},
  {id:'3',name:'未设置',language:'',eligible:true}];
const context = {window:{},document:{addEventListener:(name,fn)=>{listeners[name]=fn;}},
  navigator:{clipboard:{writeText:async value=>{copied=value;}}},crypto:{randomUUID:()=> 'test-op'},URL,console};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/youtube-publish-regular.js'),'utf8'),context);
const esc = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const ui = context.window.createYoutubeRegular({esc,shell:(_v,_title,_hint,body)=>body,
  api:(url,opts)=> url.includes('/template') ? Promise.resolve({template:{language:channels.find(c=>url.includes('/'+c.id+'/')).language}}) : request(url,opts),
  render(){},safeUrl:v=>v,top:()=>current,isOpen:v=>v===current,channels:()=>channels,
  channelState:()=>({}),defaultDescription:()=>'',toast(){}});
const row = {id:'140',channel:'1',material:{name:'Drama',language:'en'},mode:'story',title:'',
  info:{media:{width:720,height:1280,duration_ms:300000,sha256:'a'.repeat(64)}},synopsis:'Two sisters',notes:'',
  candidates:[],translationsZh:[],dirty:new Set(),templateGeneration:0,templateChannel:'1',templateStatus:'ready'};
current = {type:'regular',rows:[row]};
function click(action) { const el={dataset:{regularAction:action,regularId:'140'}};
  return listeners.click({target:{closest:()=>el}}); }
function input(field,value) { listeners.input({target:{dataset:{regularField:field,regularId:'140'},value}}); }
function result(language='ja') { return {titles:['運命の再会','駅で始まる恋','ふたりの旅'],
  translations_zh:['命运的重逢','车站开始的恋情','两人的旅程'],language,language_label:'日语',language_source:'channel'}; }

(async()=>{
  let submitted;
  request=async(url,opts)=>{submitted=JSON.parse(opts.body);return result();};
  assert.match(ui.view(current),/生成 3 个日语标题/);
  await click('titles');
  assert.equal(submitted.channel_id,'1');
  assert.match(ui.view(current),/中文：命运的重逢/);
  await click('use-0'); assert.equal(row.title,'運命の再会');
  assert.equal(row.selectedTranslation,'命运的重逢');
  await click('copy-0'); assert.equal(copied,'運命の再会');
  input('channel','2'); await Promise.resolve();
  assert.equal(row.title,''); assert.equal(row.candidates.length,0);
  assert.match(ui.view(current),/生成 3 个繁体中文标题/);

  let resolve;
  request=()=>new Promise(r=>{resolve=r;});
  const pending=click('titles');
  input('channel','1'); await Promise.resolve();
  resolve(result('zh-tw')); await pending;
  assert.equal(row.candidates.length,0); assert.equal(row.generating,false);
  let reject;
  request=()=>new Promise((_,r)=>{reject=r;});
  const failed=click('titles'); input('notes','changed');
  reject(new Error('old request failed')); await failed;
  assert.equal(row.error,'');

  input('title','My manual title'); input('channel','3'); await Promise.resolve();
  assert.equal(row.title,'My manual title');
  assert.match(ui.view(current),/频道未设置语种，使用素材语言/);
  request=async()=>({...result(),translations_zh:['','<script>alert(1)</script>','译文']});
  await click('titles');
  assert.match(ui.view(current),/中文翻译未返回/);
  assert.match(ui.view(current),/&lt;script>/);
  assert.ok(!ui.view(current).includes('<script>'));
  input('channel',''); await click('titles');
  assert.equal(row.error,'请先选择发布频道');
  console.log('Passed: channel language, paired Chinese, original-only use/copy, channel changes, stale success/failure, manual text, fallback and escaping.');
})().catch(e=>{console.error(e);process.exitCode=1;});
