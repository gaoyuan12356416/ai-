"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Run the real UI functions without booting network requests or creating promotions.
const nodes = new Map(), listeners = new Map();
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {dataset:{},value:"",innerHTML:"",outerHTML:"",addEventListener(){}});
  return nodes.get(selector);
}
const document = {querySelector:node, addEventListener:(name, fn)=>listeners.set(name,fn)};
const context = {document,window:{addEventListener(){}},location:{origin:"https://ai.yingliangads.com"},URL,TextEncoder,console};
vm.createContext(context);
let source = fs.readFileSync(path.join(__dirname,"../static/youtube-publish.js"),"utf8");
assert.match(source,/  init\(\);\s*\}\)\(\);\s*$/);
source = source.replace(/  init\(\);\s*\}\)\(\);\s*$/, "globalThis.uiTest={setDraft:value=>{draft=value;},validateText,macroPreview,materialReference};\n})();");
vm.runInContext(source, context);
const ui = context.uiTest;
const legacy = {name:"material",macro_name:"OLD".repeat(100),macro_desc:"old description",drama_cover_status:"available",drama_cover_url:"https://old.example.test/wrong-cover.jpg"};
let draft = {title:"{name}",description:"{{m_url}} {desc}",comment:"",coverSource:"ai",material:legacy,errors:{}};
ui.setDraft(draft);
assert.equal(ui.validateText("title",draft.title,legacy),"","legacy title length must not reject a Moboreels source selected in another field");
assert.ok(!ui.macroPreview("title",draft.title,legacy).includes(legacy.macro_name));
assert.match(ui.materialReference(legacy),/提交时.*Moboreels/);
assert.ok(!ui.materialReference(legacy).includes("wrong-cover.jpg"));
assert.match(ui.validateText("description","{url}",legacy),/不能.*混用/);
const confirmed = {...legacy,promotion_provider:"moboreels",macro_name:"Correct title",macro_desc:""};
assert.match(ui.validateText("description","{desc}",confirmed),/缺少/);

draft = {...draft,description:"ordinary",material:{...legacy,macro_name:"",macro_desc:""},errors:{reference:"old warning"}};
ui.setDraft(draft);
for (const field of ["title","description","comment"]) node("#draft-"+field).value=draft[field];
const description = node("#draft-description");
Object.assign(description,{id:"draft-description",dataset:{field:"description"},value:"{{m_url}}"});
listeners.get("input")({target:description});
assert.ok(!node('[data-macro-preview="title"]').outerHTML.includes("has-warning"));
assert.match(node(".drama-reference-field").innerHTML,/Moboreels 原剧封面待核验/);
assert.equal(draft.errors.reference,undefined);

description.value="ordinary";
listeners.get("input")({target:description});
assert.match(node('[data-macro-preview="title"]').outerHTML,/has-warning/);
assert.ok(node(".drama-reference-field").innerHTML.includes("wrong-cover.jpg"));
console.log("YouTube Moboreels UI tests passed: cross-field macros, metadata isolation and deferred cover verification.");
