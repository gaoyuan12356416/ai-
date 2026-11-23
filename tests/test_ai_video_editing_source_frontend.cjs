const {test}=require('node:test');
const assert=require('node:assert/strict');
const S=require('../static/video-editing-ai-source-state.js');
test('mixed selection keeps order, de-duplicates resolved catalog assets and survives serialization',()=>{
  const first={kind:'asset',asset_id:'one',name:'one'};
  const refs=[first,...Array.from({length:5},(_,i)=>({kind:'catalog',product:'dramawave',key:'drama',episode:i+1,...(!i?{asset_id:'one'}:{})}))];
  const selected=S.unique(refs);assert.equal(selected.length,5);assert.deepEqual(S.unique(JSON.parse(JSON.stringify(selected))),selected);
  const swapped=S.move(selected,4,-1);assert.equal(swapped[3].episode,5);assert.equal(swapped[4].episode,4);
  assert.equal(S.toggle(selected,{kind:'asset',asset_id:'one'}).length,4);
});
test('limit applies to distinct selected items and moving an edge is harmless',()=>{
  const items=Array.from({length:20},(_,i)=>({kind:'asset',asset_id:String(i)}));
  assert.throws(()=>S.toggle(items,{kind:'asset',asset_id:'extra'}),/20/);
  assert.equal(S.toggle(items,{kind:'asset',asset_id:'1'}).length,19);
  assert.deepEqual(S.move(items,0,-1),items);
});
