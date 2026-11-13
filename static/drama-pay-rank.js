(function(){
  'use strict';
  const $=id=>document.getElementById(id);
  const names={en:'英语',es:'西班牙语',pt:'葡萄牙语',ar:'阿拉伯语',bn:'孟加拉语',cs:'捷克语',de:'德语',el:'希腊语',fr:'法语',hi:'印地语',hu:'匈牙利语',id:'印度尼西亚语',it:'意大利语',ja:'日语',ko:'韩语',ms:'马来语',nb:'挪威语',nl:'荷兰语',pl:'波兰语',ro:'罗马尼亚语',ru:'俄语',sv:'瑞典语',ta:'泰米尔语',te:'泰卢固语',th:'泰语',tl:'菲律宾语',tr:'土耳其语',uk:'乌克兰语',vi:'越南语','zh-TW':'繁体中文'};
  let auth={},generation=0;
  function el(tag,text,className){const n=document.createElement(tag);n.textContent=text;if(className)n.className=className;return n;}
  async function api(url){const r=await fetch(url,{credentials:'same-origin',cache:'no-store'});const d=await r.json();if(!r.ok)throw new Error(r.status===401?'请先使用飞书登录':r.status===403?'请联系管理员开通“短剧付费 TOP100”权限':'榜单暂时无法读取，请稍后重试');return d;}
  function options(select,values,selected,label){select.replaceChildren(...values.map(v=>{const o=el('option',label(v));o.value=v;return o;}));select.value=selected;}
  async function load(){
    const stamp=++generation;const day=$('dateFilter').value;const lang=$('languageFilter').value||'en';
    $('notice').hidden=true;
    try{
      const {data:d}=await api('/api/drama-pay-rank/list?'+new URLSearchParams({date:day,language_code:lang}));if(stamp!==generation)return;
      options($('dateFilter'),['',...d.dates],day,v=>v||'最新榜单');
      options($('languageFilter'),Array.from(new Set(['en',...d.languages])),lang,v=>(names[v]||v)+' · '+v);
      $('windowLabel').textContent=d.date?`榜单 ${d.date} · 统计 ${d.window_start} 至 ${d.window_end}（UTC）`:'尚无榜单';
      $('updatedLabel').textContent=d.fetched_at?'采集时间 '+new Date(d.fetched_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（北京）':'';
      $('countLabel').textContent=d.list.length+' 部短剧';
      $('rows').replaceChildren();
      for(const row of d.list){const tr=el('tr','');const rank=el('td','');rank.append(el('span',row.rank,'rank'+(row.rank<=3?' top':'')));const drama=el('td','');drama.append(el('div',row.name||'剧名待补充','drama-name'),el('div',row.series_key,'drama-id'));const audience=el('td','');audience.append(el('span',row.audience||'待补充','badge'));tr.append(rank,drama,el('td',names[row.language_code]||row.language_code),audience);$('rows').append(tr);}
      if(!d.list.length){const tr=el('tr','');const td=el('td',d.availability==='language_empty'?'该语种当天没有榜单结果':'该日期榜单尚未采集或尚未产出','empty');td.colSpan=4;tr.append(td);$('rows').append(tr);}
      const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
      if(d.status.status!=='ok'||(!day&&d.date&&d.date<today)){$('notice').textContent='今日榜单尚未更新成功，当前展示最近可用结果。';$('notice').hidden=false;}
    }catch(e){if(stamp!==generation)return;$('notice').textContent=e.message;$('notice').hidden=false;$('rows').replaceChildren();$('windowLabel').textContent='读取失败';}
  }
  $('filters').addEventListener('submit',e=>{e.preventDefault();load();});
  $('loginBtn').addEventListener('click',()=>UiTopbar.handleAuthAction({auth}));
  async function init(){try{const top=await api('/api/ui/topbar');auth=top.auth||top;UiTopbar.render({auth,userCard:'#userCard',authButton:'#loginBtn',refreshButton:'#refreshBtn'});await QuickNav.render({container:'#quickNav',activeKey:'dramaPayRank',auth});await load();}catch(e){$('notice').textContent=e.message;$('notice').hidden=false;}}
  init();
})();
