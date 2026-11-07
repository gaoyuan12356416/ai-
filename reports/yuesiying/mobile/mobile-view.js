/* View-only enhancement: labels, navigation and dialog scrolling. */
(()=>{'use strict';
 const nav=document.createElement('nav');nav.className='mobile-nav';nav.setAttribute('aria-label','手机版快捷导航');
 for(const [id,label] of [['top','顶部'],['filters','筛选'],['productDesk','分产品'],['materialPanel','素材']]){const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=()=>{if(id==='top')window.scrollTo({top:0,behavior:'smooth'});else (id==='filters'?document.querySelector('.controls'):document.getElementById(id))?.scrollIntoView({block:'start',behavior:'smooth'})};nav.append(b)}
 document.body.append(nav);
 let scheduled=false;
 function decorate(){scheduled=false;
  for(const cell of document.querySelectorAll('#productCards .stage-cell')){if(cell.parentElement?.classList.contains('mobile-stage'))continue;const text=cell.querySelector('button')?.textContent.replace(/\s+/g,' ').trim()||'阶段剧目';const box=document.createElement('details');box.className='mobile-stage';const summary=document.createElement('summary');summary.textContent=text.slice(0,80)+' · 展开剧目';box.append(summary);cell.before(box);box.append(cell)}
  for(const table of document.querySelectorAll('main table:not(.matrix),dialog table')){
   const headers=[...table.querySelectorAll('thead th')].map(th=>{const copy=th.cloneNode(true);copy.querySelectorAll('input').forEach(e=>e.remove());copy.querySelectorAll('br').forEach(e=>e.replaceWith(' '));return copy.textContent.replace(/\s+/g,' ').trim()});
   if(!headers.length)continue;table.classList.add('mobile-card-table');
   for(const tr of table.querySelectorAll('tbody tr'))[...tr.children].forEach((td,i)=>{const label=headers[i]||'';td.dataset.mobileLabel=label;if(/名称|短剧ID|素材库ID|ads_source|资源|对应来源|创建时间|当前状态/.test(label)||td.colSpan>1)td.dataset.mobileWide='';else delete td.dataset.mobileWide});
  }
  for(const dialog of document.querySelectorAll('dialog')){const hint=[...dialog.querySelectorAll('div')].find(x=>!x.childElementCount&&x.textContent.includes('指标表可左右滑动。'));if(hint&&matchMedia('(max-width:950px)').matches)hint.textContent=hint.textContent.replace('指标表可左右滑动。','指标按卡片排列，向下查看。');if(dialog.querySelector('.mobile-dialog-nav'))continue;const bar=document.createElement('div');bar.className='mobile-dialog-nav';
   for(const [label,action] of [['选择区',()=>dialog.scrollTo({top:0,behavior:'smooth'})],['复制区',()=>{const target=dialog.querySelector('[data-output],[data-copy="ids"],[data-copy]');(target?.closest('.low-footer')||target)?.scrollIntoView({block:'start',behavior:'smooth'})}],['关闭',()=>dialog.querySelector('[data-close]')?.click()]]){const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=action;bar.append(b)}dialog.prepend(bar);
  }
 }
 new MutationObserver(()=>{if(!scheduled){scheduled=true;requestAnimationFrame(decorate)}}).observe(document.body,{childList:true,subtree:true});decorate();
})();
