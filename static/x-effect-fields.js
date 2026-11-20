(() => {
  'use strict';
  const metrics={views:'X 浏览量',likes:'X 点赞量',comments:'X 评论量',shares:'X 分享量',revenue:'X 收入（USD）'};
  function mount(){
    for(const level of ['drama','material']){
      const select=document.getElementById(level+'SortBy');if(!select||document.getElementById(level+'XEffects'))continue;
      const box=document.createElement('fieldset');box.id=level+'XEffects';box.style='border:1px solid #dbe4ef;border-radius:12px;padding:16px;margin:16px 0';
      const title=document.createElement('legend');title.textContent='X 渠道累计效果';box.append(title);
      const note=document.createElement('p');note.className='helper';note.textContent='汇总 X 全部账号；分享 = 转发 + 引用。启用效果条件或排序后，可复用其他账号已发布的素材，同一账号不重复。';box.append(note);
      for(const [metric,label] of Object.entries(metrics)){
        select.add(new Option(label,'x_'+metric));
        const group=document.createElement('label');group.className='field';group.style='display:block;margin:10px 0';
        const text=document.createElement('span');text.textContent=label;group.append(text);
        const range=document.createElement('div');range.className='range-inputs';range.style='display:flex;gap:8px;margin-top:5px';
        for(const [bound,placeholder] of [['min','最小值（不限）'],['max','最大值（不限）']]){
          const input=document.createElement('input');input.type='number';input.min='0';input.step=metric==='revenue'?'0.01':'1';input.id=level+'_x_'+metric+'_'+bound;input.placeholder=placeholder;input.setAttribute('aria-label',label+placeholder);input.style='width:48%;padding:9px;border:1px solid #ccd6e2;border-radius:8px';range.append(input);
        }group.append(range);box.append(group);
      }
      (select.closest('.rule-card')||select.parentElement.parentElement).append(box);
    }
  }
  function fill(level,data){mount();for(const metric of Object.keys(metrics))for(const bound of ['min','max']){
    const key='x_'+metric+'_'+bound;document.getElementById(level+'_'+key).value=data[key]??'';
  }}
  function read(level){const data={};for(const metric of Object.keys(metrics))for(const bound of ['min','max']){
    const key='x_'+metric+'_'+bound;const value=document.getElementById(level+'_'+key).value.trim();data[key]=value===''?null:value;
  }return data;}
  window.XEffectFields={mount,fill,read,metrics};mount();
})();
