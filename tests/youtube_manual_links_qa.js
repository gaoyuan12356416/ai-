/* Isolated browser/API mocks. Run with playwright-cli run-code --filename. */
async (page) => {
  const checks=[],errors=[],posts=[],history=new Map();
  const check=(name,ok)=>{if(!ok)throw new Error(name);checks.push(name);};
  const reply=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  let mode='normal',held=null,delaySearch=false,heldSearch=null;
  const channels=[{id:'12',name:'DramaWave English',eligible:true},{id:'13',name:'授权过期频道',eligible:false,reason:'授权已失效，请重新授权'}];
  const dramas=[{content_id:'Abc1234567',language:'en',name:'The Last Promise',selectable:true},{content_id:'Xyz1234567',language:'zh-tw',name:'最後的承諾',selectable:true}];
  await page.unrouteAll({behavior:'ignoreErrors'});page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',async route=>{
    const req=route.request(),parts=req.url().replace(/^https?:\/\/[^/]+/,'').split('?'),path=parts[0];
    const query=Object.fromEntries((parts[1]||'').split('&').filter(Boolean).map(p=>p.split('=').map(v=>decodeURIComponent(v.replace(/\+/g,' ')))));
    if(path==='/api/ui/topbar')return reply(route,{authenticated:true,user:{name:'测试发布人',role:'admin',is_admin:true,tenant_key:'qa',permissions:{youtube_auto_publish:true}}});
    if(path.endsWith('/bootstrap'))return reply(route,{settings:{default_description:'Watch more exciting short dramas on DramaWave'},source:{configured:false},channels_loaded:false,enabled:true,can_manage_settings:true});
    if(path.endsWith('/tasks'))return reply(route,{items:[],counts:{all:0},total:0});
    if(path.endsWith('/short-links/channels'))return reply(route,{channels,checking:false});
    if(path.endsWith('/short-links/dramas')){
      if(delaySearch){heldSearch=route;return;}
      if(query.search==='fail')return reply(route,{message:'剧库暂不可用'},503);
      return reply(route,{items:dramas,page:Number(query.page),has_more:query.page==='1'});
    }
    if(path.endsWith('/short-links')&&req.method()==='POST'){
      const p=req.postDataJSON();posts.push(p);
      const link=history.get(p.operation_id)||{operation_id:p.operation_id,status:'published',short_url:'https://gy.g2flow.com/s2l/youtube/'+(history.size+501)+'.html'};
      history.set(p.operation_id,link);
      if(mode==='held'){held=route;return;}
      if(mode==='lost')return route.abort();
      if(mode==='retry')return reply(route,{message:'短链暂未完成'},503);
      return reply(route,{link});
    }
    if(/\/short-links\/[0-9a-f-]{36}$/.test(path)){const link=history.get(path.split('/').pop());return reply(route,link?{link}:{message:'not found'},link?200:404);}
    if(path.endsWith('/channels'))return reply(route,{channels,checking:false});
    if(path.endsWith('/materials'))return reply(route,{items:[],configured:false});
    return reply(route,{message:'Unexpected '+path},404);
  });
  await page.setViewportSize({width:1440,height:1000});await page.goto('http://127.0.0.1:8893/youtube-publish.html');
  await page.locator('#short-link-button').waitFor();await page.locator('#short-link-button').click();
  await page.waitForFunction(()=>document.querySelector('#manual-link-channel option[value="12"]'));
  check('dialog opens without configured material SQL',await page.locator('.manual-link-dialog').isVisible());
  check('blocked channel disabled',await page.locator('#manual-link-channel option[value="13"]').isDisabled());
  check('generation requires selections',await page.locator('[data-link-action="generate"]').isDisabled());
  await page.locator('#manual-link-channel').selectOption('12');await page.locator('#manual-link-search').fill('The');
  await page.locator('[name="manual-link-drama"]').first().waitFor();await page.locator('[name="manual-link-drama"]').first().check();
  check('name language and content ID visible',(await page.locator('#manual-link-selection').textContent()).includes('Abc1234567'));
  const modal=await page.locator('.manual-link-dialog').elementHandle();const search=await page.locator('#manual-link-search').elementHandle();
  await page.waitForTimeout(6300);
  check('task poll preserves mounted dialog and search',await page.evaluate(([m,s])=>m===document.querySelector('.manual-link-dialog')&&s===document.querySelector('#manual-link-search'),[modal,search]));
  mode='held';await page.locator('[data-link-action="generate"]').click();await page.waitForFunction(()=>document.querySelector('#manual-link-channel').disabled);
  check('double-click blocked while generating',posts.length===1&&await page.locator('[data-link-action="generate"]').isDisabled());
  await page.locator('[data-link-action="close"]').last().click();await reply(held,{link:history.get(posts[0].operation_id)});held=null;mode='normal';
  await page.waitForTimeout(100);check('late success never reopens dialog',!await page.locator('.manual-link-dialog').isVisible());
  await page.locator('#short-link-button').click();await page.waitForFunction(()=>!!document.querySelector('#manual-link-url').value);
  const first=await page.locator('#manual-link-url').inputValue();check('close and reopen keeps result',first.endsWith('/501.html'));
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async v=>{window.copiedManualLink=v;}}}));
  await page.locator('[data-link-action="copy"]').click();check('copy returns exact link',await page.evaluate(()=>window.copiedManualLink)===first);
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('denied');}}}));
  await page.locator('[data-link-action="copy"]').click();check('copy denial offers selected text',await page.locator('#manual-link-copy-hint').textContent()==='自动复制不可用，链接已选中，请手动复制。');
  await page.screenshot({path:'output/playwright/youtube-manual-links-desktop.png',fullPage:true});
  await page.locator('[data-link-action="generate"]').click();await page.waitForFunction(()=>document.querySelector('#manual-link-url').value.endsWith('/502.html'));
  check('explicit new generation has new operation and URL',posts.length===2&&posts[0].operation_id!==posts[1].operation_id);
  await page.locator('[name="manual-link-drama"]').nth(1).check();check('selection change clears previous result',await page.locator('#manual-link-result').isHidden());
  mode='lost';await page.locator('[data-link-action="generate"]').click();await page.locator('[data-link-action="check"]').waitFor();
  check('unknown response locks original selection',await page.locator('#manual-link-channel').isDisabled());
  const count=posts.length;mode='normal';await page.locator('[data-link-action="check"]').click();await page.waitForFunction(()=>document.querySelector('#manual-link-url').value.endsWith('/503.html'));
  check('readback recovers original operation without another POST',posts.length===count);
  mode='retry';await page.locator('[data-link-action="generate"]').click();await page.locator('[data-link-action="check"]').waitFor();
  const original=posts.at(-1).operation_id;mode='normal';await page.locator('[data-link-action="generate"]').click();await page.waitForFunction(()=>document.querySelector('#manual-link-url').value.endsWith('/504.html'));
  check('retry retains same operation',posts.at(-1).operation_id===original&&history.size===4);
  await page.locator('#manual-link-search').fill('fail');await page.locator('[data-link-action="search"]').waitFor();check('search failure has retry',await page.locator('[data-link-action="search"]').isVisible());
  await page.locator('#manual-link-search').fill('The');await page.locator('[name="manual-link-drama"]').first().waitFor();
  await page.locator('[data-link-action="next"]').click();await page.waitForFunction(()=>document.querySelector('#manual-link-page').textContent==='第 2 页');check('pagination',await page.locator('[data-link-action="next"]').isDisabled());
  await page.locator('[name="manual-link-drama"]').first().check();
  await page.setViewportSize({width:390,height:844});check('mobile no horizontal overflow',await page.evaluate(()=>document.querySelector('.manual-link-dialog').scrollWidth<=innerWidth));
  await page.screenshot({path:'output/playwright/youtube-manual-links-mobile.png',fullPage:true});
  await page.keyboard.press('Escape');check('Escape closes dialog',!await page.locator('.manual-link-dialog').isVisible());
  await page.setViewportSize({width:1440,height:1000});await page.locator('#settings-button').click();check('existing default settings still open',await page.locator('#default-description').isVisible());await page.locator('[data-action="close-modal"]').last().click();
  await page.locator('#new-publish').click();check('existing publishing dialog still opens',await page.locator('#draft-title').isVisible());
  check('no browser runtime errors',errors.length===0);
  return {passed:checks.length,checks,errors};
}
