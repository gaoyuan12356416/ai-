'use strict';
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const test = require('node:test');
const model = require('../static/video-editing.js');
const clip = (id, source_id, start_ms, end_ms, transition='cut') => ({ id, source_id, start_ms, end_ms, transition, fit:'contain', mute:false });
const source = (duration_ms=20000) => ({ status:'ready', metadata:{duration_ms} });

test('replication validates actual source duration and target language membership without excluding source language',()=>{
  const context={enabled:true,can_change_operator:true,source:{duration_ms:1789867,source_language:'en'},target_languages:[{code:'en'},{code:'es'}]};
  const payload={task_type:1,audio_type:0,target_material_type:1,target_langs:['en'],operator:'owner',no_bgm:false};
  assert.equal(model.validateReplicationInput(payload,context),'');
  assert.match(model.validateReplicationInput({...payload,target_langs:[]},context),/至少选择/);
  assert.match(model.validateReplicationInput({...payload,target_langs:['unsupported']},context),/列表已变化/);
  assert.equal(model.replicationTimeMs('00:29:49.867'),1789867);
  assert.equal(model.replicationTimeMs('00:60:00'),null);
  const narration={...payload,target_material_type:2,descant_start_time:'00:00:00',descant_end_time:'00:29:49.867'};
  assert.equal(model.validateReplicationInput(narration,context),'');
  assert.match(model.validateReplicationInput({...narration,descant_end_time:'00:29:49.868'},context),/不得|不能/);
  assert.match(model.validateReplicationInput({...narration,descant_start_time:'00:29:49.867'},context),/开始时间/);
  assert.match(model.validateReplicationInput(payload,{...context,can_change_operator:false,default_operator:''}),/归属账号/);
});

test('replication receipt never treats accepted or source transfer as finished output',()=>{
  assert.equal(model.replicationReceipt({status:'accepted',upstream_status:'waiting_download'}).label,'已受理 · 受理时：待转存');
  assert.match(model.replicationReceipt({status:'accepted',upstream_status:'completed'}).message,/尚未查询/);
  assert.equal(model.replicationReceipt({status:'unknown_outcome'}).label,'结果待核实');
  assert.equal(model.replicationReceipt({status:'partial'}).label,'部分语种已受理');
  assert.equal(model.replicationReceipt({status:'rejected'}).label,'提交失败');
});

test('preview geometry follows verified FFmpeg even-pixel scaling and chroma offsets',()=>{
  assert.deepEqual(model.fitRectangle(1920,1080,'contain'),{x:0,y:436,width:720,height:406});
  assert.deepEqual(model.fitRectangle(638,480,'contain'),{x:0,y:368,width:720,height:542});
  assert.deepEqual(model.fitRectangle(640,480,'cover'),{x:-492,y:-0,width:1706,height:1280});
  assert.deepEqual(model.fitRectangle(1920,1080,'cover'),{x:-778,y:-0,width:2276,height:1280});
  assert.deepEqual(model.effectiveBand({mirror:true,subtitle_band:{top:959,bottom:1279}}),{top:960,bottom:1280});
  assert.equal(model.effectiveBand({mirror:true,subtitle_band:null}),null);
});

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
test('episode progress distinguishes queue, bytes, processing and ready state',()=>{
  assert.match(model.probeDisplay({status:'probing',probe_progress:{status:'queued',stage:'queued'}}).text,/排队中/);
  const p=model.probeDisplay({status:'probing',probe_progress:{status:'running',stage:'downloading',download_bytes:12582912,total_bytes:41943040}});
  assert.equal(p.percent,30);assert.match(p.text,/12.0 \/ 40.0 MB/);
  assert.equal(model.probeDisplay({status:'probing',probe_progress:{stage:'downloading',download_bytes:10,total_bytes:0}}).percent,null);
  assert.match(model.probeDisplay({status:'probing',probe_progress:{status:'unknown',stage:'downloading'}}).text,/核实/);
  assert.match(model.probeDisplay({status:'ready',metadata:{duration_ms:103733},probe_progress:{stage:'downloading'}}).text,/103.733 秒 · 已就绪/);
});

test('v2 draft serialization retains incomplete units, multiple assets and mirror settings without a global asset',()=>{
  const units=[{id:'unit_a',series_code:'{v3404/l144250/}',title:'English title',clips:[{...clip('a','asset1',0,1000),mirror:true,subtitle_band:{top:800,bottom:1000}},clip('b','asset2',0,2000)]},{id:'unit_b',series_code:'',title:'',clips:[]}];
  const project={schema_version:2,name:'Draft',dramas:units,asset_id:'stale',units:[{id:'stale'}]};
  const saved=model.draftForSave(project);
  assert.deepEqual(Object.keys(saved).sort(),['name','schema_version','units']);assert.deepEqual(saved.units,units);assert.equal(saved.units[1].series_code,'');
  saved.units[0].clips[0].subtitle_band.top=400;assert.equal(units[0].clips[0].subtitle_band.top,800);
});

test('old drafts use read-only upgrade units and preserve unresolvable timelines',()=>{
  const old={name:'Old',dramas:[{app_id:'2310',content_id:'DramaA',title:'A',clips:[clip('a','legacy',1,9000)]}]};
  const units=model.projectUnits(old);assert.equal(units[0].id,'unit_legacy_0');assert.equal(units[0].series_code,'');assert.deepEqual(units[0].clips,old.dramas[0].clips);assert.equal(old.schema_version,undefined);
  const upgraded=model.projectUnits({...old,upgrade_units:[{id:'unit_old',series_code:'21481',title:'English A',clips:old.dramas[0].clips}]});assert.equal(upgraded[0].series_code,'21481');
});

test('split keeps mirror and subtitle band independently on both halves',()=>{
  const original={...clip('a','source',0,10000),mirror:true,subtitle_band:{top:800,bottom:1000}};
  const cuts=model.split([original],'a',5000);assert.ok(cuts.every(c=>c.mirror));assert.deepEqual(cuts.map(c=>c.subtitle_band),[{top:800,bottom:1000},{top:800,bottom:1000}]);assert.equal(original.end_ms,10000);
  assert.deepEqual(model.effectiveBand({mirror:true}),{top:960,bottom:1280});assert.equal(model.effectiveBand({mirror:true,subtitle_band:null}),null);assert.equal(model.effectiveBand({mirror:false}),null);
  assert.deepEqual(model.effectiveBand({mirror:true,subtitle_band:{top:801,bottom:1001}}),{top:802,bottom:1002});
});
