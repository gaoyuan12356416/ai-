'use strict';
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const test = require('node:test');
const model = require('../static/video-editing.js');
const clip = (id, source_id, start_ms, end_ms, transition='cut') => ({ id, source_id, start_ms, end_ms, transition, fit:'contain', mute:false });
const source = (duration_ms=20000) => ({ status:'ready', metadata:{duration_ms} });

test('COS Content-MD5 matches independent crypto oracle across padding boundaries and full 16 MiB parts', () => {
  for (const length of [0,1,3,55,56,57,63,64,65,127,128,129,16384,16777216]) {
    const bytes=Buffer.alloc(length);for(let i=0;i<length;i++)bytes[i]=(i*17+113)%256;
    assert.equal(model.md5Base64(bytes),crypto.createHash('md5').update(bytes).digest('base64'),`length ${length}`);
  }
  assert.equal(model.md5Base64(Buffer.from('abc')),'kAFQmDzST7DWlj99KOF/cg==');
});

test('timeline clocks subtract each crossfade overlap exactly once', () => {
  const clips=[clip('a','asset',0,5000,'fade'),clip('b','ep1',2000,12000,'fade'),clip('c','ep2',0,7000)];
  assert.equal(model.totalDuration(clips),21400);
  const track=model.layout(clips);
  assert.deepEqual(track.map(item=>[item.start,item.end]),[[0,5000],[4700,14700],[14400,21400]]);
  assert.equal(model.totalDuration([]),0);
});

test('middle deletion after two cuts preserves exact source timing and tail transition', () => {
  const original=[clip('ep','ep1',2000,18000,'fade'),clip('asset','asset',0,5000)];
  const first=model.split(original,'ep',7000);
  assert.equal(first[0].transition,'cut');assert.equal(first[1].transition,'fade');
  const second=model.split(first,first[1].id,12000);
  const kept=second.filter((_,index)=>index!==1);
  assert.deepEqual(kept.map(item=>[item.source_id,item.start_ms,item.end_ms,item.transition]),[['ep1',2000,7000,'cut'],['ep1',12000,18000,'fade'],['asset',0,5000,'cut']]);
  assert.equal(model.totalDuration(kept),15700);
  assert.equal(original.length,2);assert.equal(original[0].end_ms,18000);
  assert.throws(()=>model.split(original,'ep',2000),/片段内部/);
  assert.throws(()=>model.split(original,'ep',19000),/片段内部/);
});

test('short clips reject two-sided fades instead of silently changing effects', () => {
  const clips=[clip('a','s1',0,1000,'fade'),clip('b','s2',0,567,'fade'),clip('c','s3',0,1000)];
  assert.match(model.validateClips(clips,{s1:source(),s2:source(),s3:source()}).join(','),/第 2 个片段按 30fps 计算不足 0.6 秒/);
  clips[1].end_ms=600;
  assert.deepEqual(model.validateClips(clips,{s1:source(),s2:source(),s3:source()}),[]);
  assert.equal(clips[1].transition,'fade');
});

test('displayed output length and clip validity follow the GPU 30fps endpoint rounding',()=>{
  assert.equal(model.toFrame(16),0);assert.equal(model.toFrame(17),1);
  assert.equal(model.totalDuration([clip('a','s',17,1017)]),1000);
  assert.equal(model.totalDuration([clip('a','s',0,17)]),1000/30);
  assert.match(model.validateClips([clip('a','s',0,1)],{s:source()}).join(','),/不足一帧/);
});

test('invalid source metadata and trim ranges fail with the selected clip location', () => {
  const errors=model.validateClips([clip('a','s',100,2000)],{s:{status:'probing',metadata:{duration_ms:1500}}});
  assert.match(errors.join(';'),/第 1 个片段尚未通过媒体校验/);
  assert.match(errors.join(';'),/第 1 个片段超出原视频时长/);
  assert.match(model.validateClips([clip('a','s',2000,1000)],{}).join(';'),/保留范围无效/);
  assert.match(model.validateClips([clip('a','s',0,1000,'fade')],{}).join(';'),/最后一个片段/);
});

test('applying common asset placement preserves each drama episode trims and creates unique references', () => {
  const first=clip('a1','ep1',2000,5500),second=clip('a2','ep1',9000,14000);
  const dramas=[{title:'first',clips:[clip('asset-old','asset',0,3000),first,second]},{title:'second',clips:[clip('b1','ep2',7000,11000)]}];
  const updated=model.applyCommon(dramas,clip('template','asset',500,2500),'end','fade','asset');
  assert.deepEqual(updated[0].clips.slice(0,2).map(c=>[c.source_id,c.start_ms,c.end_ms]),[['ep1',2000,5500],['ep1',9000,14000]]);
  assert.deepEqual(updated[1].clips.slice(0,1).map(c=>[c.source_id,c.start_ms,c.end_ms]),[['ep2',7000,11000]]);
  assert.equal(updated[0].clips.at(-1).transition,'cut');assert.equal(updated[0].clips.at(-1).start_ms,500);
  assert.notEqual(updated[0].clips.at(-1).id,updated[1].clips.at(-1).id);
  assert.equal(dramas[0].clips[0].id,'asset-old');
});

test('transition-only common rule retains repeated material references and episode cuts', () => {
  const clips=[clip('a','asset',0,1000),clip('b','ep1',5000,9000),clip('c','asset',500,2000)];
  const updated=model.applyCommon([{clips}],null,'none','fade','asset')[0].clips;
  assert.deepEqual(updated.map(c=>[c.id,c.source_id,c.start_ms,c.end_ms]),clips.map(c=>[c.id,c.source_id,c.start_ms,c.end_ms]));
  assert.deepEqual(updated.map(c=>c.transition),['fade','fade','cut']);
});

test('reorder preserves media boundaries and strips an impossible last transition', () => {
  const clips=[clip('a','ep1',1000,5000,'fade'),clip('b','ep2',2000,7000)];
  const reordered=model.move(clips,0,1);
  assert.deepEqual(reordered.map(c=>[c.id,c.start_ms,c.end_ms,c.transition]),[['b',2000,7000,'cut'],['a',1000,5000,'cut']]);
  assert.equal(clips[0].transition,'fade');
});
