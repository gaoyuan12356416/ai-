const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname,'..');
const read = file => fs.readFileSync(path.join(root,'static',file),'utf8');
let inserted = '';
const box = {value:'',selectionStart:0,selectionEnd:0,setRangeText:v=>{inserted=v;},focus(){},dispatchEvent(){}};
const ctx = vm.createContext({window:{},document:{getElementById:()=>box},TextEncoder,URL,Event:class {},Intl,Date,console});
new vm.Script(read('x-video-account-settings.js')).runInContext(ctx);
const P = ctx.window.XVideoPublish;
assert.equal(P.templateError('Watch {{m_url}}'),'');
assert.equal(P.templateError('{name} {desc} {url}'),'');
assert.notEqual(P.templateError('{m_url}'),'');
assert.notEqual(P.templateError('{{{m_url}}}'),'');
P.insertMacro({dataset:{xpTarget:'caption',xpMacro:'m_url'}});
assert.equal(inserted,'{{m_url}}');
assert.ok(P.macros('caption').includes('{{m_url}}'));
let html=read('tt-post-pool.html');
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
  if(match[1].trim()) new vm.Script(match[1]);
}
let caption='{{m_url}} {{drama_name}} {desc}';
const tc=vm.createContext({
  CAPTION_PLACEHOLDER_PATTERN:/\{\{([^{}]*)\}\}/g,
  CAPTION_PLACEHOLDERS:new Set(['contect_id','content_id','drama_name','m_url']),
  CAPTION_SINGLE_PLACEHOLDER_PATTERN:/\{([^{}]*)\}/g,
  CAPTION_URL_PREVIEW:'https://example.test/old',CAPTION_CODE_PREVIEW:'1234',
  cleanId:x=>String(x),normalizeDramaDescription:x=>String(x||''),normalizeDramaName:x=>String(x||''),
  utf16Units:x=>String(x).length,byId:()=>({value:caption}),state:{materials:[]},
});
const start=html.indexOf('function renderCaptionTemplate('),end=html.indexOf('function updateCaptionState()',start);
new vm.Script(html.slice(start,end)).runInContext(tc);
assert.equal(tc.renderCaptionTemplate(caption,'42','Intro','Title'),'https://ai.yingliangads.com/m/preview Title Intro');
assert.equal(tc.captionTemplateValidation([{material_id:'1',content_id:'42',drama_name:'Title',description:'Intro'}]).error,'');
caption='{m_url}';assert.notEqual(tc.captionTemplateValidation().error,'');
caption='{{ m_url }}';assert.notEqual(tc.captionTemplateValidation().error,'');
caption='{{m_url}} {{drama_name}}';
assert.notEqual(tc.captionTemplateValidation([{material_id:'1',content_id:'42'}]).error,'');
console.log('Social macro UI tests passed: exact token, insertion, preview and malformed tokens.');
