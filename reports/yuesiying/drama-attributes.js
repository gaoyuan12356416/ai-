(function(root){
'use strict';
function describe(meta,today){
  const m=meta||{},published=typeof m.published==='string'?m.published.slice(0,10):'';
  const day=/^\d{4}-\d{2}-\d{2}$/.test(today||'')?Date.parse(today+'T00:00:00Z'):NaN;
  const first=/^\d{4}-\d{2}-\d{2}$/.test(published)?Date.parse(published+'T00:00:00Z'):NaN;
  const age=(day-first)/86400000;
  const launch=Number.isInteger(m.age)&&m.age>=0&&m.age===age?'首发第'+(age+1)+'天':'首发天数待核实';
  const audio=m.audio_type===0?'非配音剧':m.audio_type===1?'配音剧·人工':m.audio_type===2?'配音剧·AI':'配音待核实';
  const code=typeof m.language==='string'?m.language.trim():'';
  let language='语种待核实';
  if(m.segment==='en'&&code.toLowerCase()==='en')language='英语（en）';
  else if(m.segment==='小语种'&&code&&code!=='待核实'&&code.toLowerCase()!=='en'){
    let name=code;try{name=new Intl.DisplayNames(['zh-CN'],{type:'language'}).of(code)||code}catch(e){}
    language='小语种·'+name+'（'+code+'）';
  }
  return [launch,audio,language];
}
if(typeof module!=='undefined'&&module.exports)module.exports={describe};
if(!root.document||!root.StageMaterialConfirm)return;
const original=root.StageMaterialConfirm.open;
root.StageMaterialConfirm.open=function(items,scope){
  const result=original.call(this,items,scope),dialog=document.getElementById('materialConfirmDialog');
  if(!dialog)return result;
  const data=typeof DATA!=='undefined'?DATA:null,groups=new Map();
  for(const item of items){const key=JSON.stringify([item.language,item.cid,item.resource_name]);if(!groups.has(key))groups.set(key,item)}
  const labels=[...groups.values()].map(item=>{
    const m=data?.meta?.[item.cid];
    // Only the exact Content ID directory entry is authoritative; never parse names.
    return describe(m&&String(m.cid)===String(item.cid)?m:null,data?.today);
  });
  const style=document.createElement('style');style.textContent='#materialConfirmDialog .confirm-attributes{display:flex;gap:5px;flex-wrap:wrap;margin-top:7px;font-size:11px;line-height:1.5}#materialConfirmDialog .confirm-attributes span,#materialConfirmDialog [data-batch-attributes]{display:inline-block;padding:3px 6px;border:1px solid #cbd9ed;border-radius:6px;background:#f0f5ff;color:#3d5b83;overflow-wrap:anywhere}#materialConfirmDialog [data-batch-attributes]{margin:5px 0 0 10px;font-size:12px;line-height:1.6;vertical-align:middle;max-width:100%;box-sizing:border-box}';dialog.append(style);
  const batch=document.createElement('span');batch.dataset.batchAttributes='';
  const same=labels.length>0&&labels.every(x=>JSON.stringify(x)===JSON.stringify(labels[0]));
  batch.textContent='本批剧目属性：'+(!labels.length?'暂无剧目':same?labels[0].join('｜'):'混合批次 · 各剧属性见左侧');
  batch.title='按短剧ID目录判断；首发天数以报表截止日（'+(data?.today||'待核实')+'，UTC+8）为准，上架当天为第1天，不代表当前可投。';
  dialog.querySelector('[data-copy-type]')?.after(batch);
  [...dialog.querySelectorAll('.confirm-drama-head')].forEach((head,i)=>{
    const box=document.createElement('div');box.className='confirm-attributes';box.dataset.dramaAttributes=String(i);
    for(const text of labels[i]||describe(null,data?.today)){const badge=document.createElement('span');badge.textContent=text;box.append(badge)}
    box.title=batch.title;head.querySelector('.confirm-cid')?.after(box);
  });
  return result;
};
})(typeof globalThis!=='undefined'?globalThis:this);
