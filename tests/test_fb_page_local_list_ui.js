// Playwright CLI: run-code --filename tests/test_fb_page_local_list_ui.js
// Entirely mocked transport. No production session, mutation, or publish request.
async page => {
  await page.unrouteAll({behavior:"ignoreErrors"});
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const items = Array.from({length:3910}, (_, i) => ({page_id:String(1000+i),page_name:`Page ${i}`,
    language:'en',caption_template:'edited caption',version:7,created_by:'creator',created_by_name:'Creator',
    authorization_pending:true,authorization_unknown:true,eligible_token_count:0,blocked_reason:''}));
  let calls=[],lists=0,mode='busy',held=false,releaseOld;
  const reply=(route, data, status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  await page.route('https://fb-list.test/**',async route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/fb-video-publish.js')return route.fulfill({path:'static/fb-video-publish.js',contentType:'text/javascript; charset=utf-8'});
    if(url.pathname.endsWith('.html'))return route.fulfill({contentType:'text/html',body:`<meta charset="utf-8"><body data-fb-view="${url.pathname==='/publish.html'?'publish':'pages'}"><div id="fbApp"></div><script>window.QuickNav={render(){}};window.UiTopbar={render(){}};</script><script src="/fb-video-publish.js"></script></body>`});
    if(url.pathname==='/api/ui/topbar')return reply(route,{ok:true,authenticated:true,user:{is_admin:false,permissions:{fb_video_publish:true}}});
    if(url.pathname.endsWith('/languages'))return reply(route,{ok:true,items:[{value:'en',label:'英语'}]});
    if(url.pathname.endsWith('/draft'))return reply(route,{ok:true,draft:null});
    if(url.pathname.endsWith('/pages')){
      if(!url.searchParams.has('verify_ids')){lists++;return reply(route,{ok:true,items,authorization_pending:true});}
      const ids=url.searchParams.get('verify_ids').split(',');calls.push(ids);
      assert(ids.length<=8,'verification exceeded 8 IDs');
      if(mode==='busy')return reply(route,{ok:false,code:'source_busy',message:'素材查询繁忙'},503);
      let stale=false;
      if(mode==='hold'&&!held){held=true;stale=true;await new Promise(resolve=>releaseOld=resolve);}
      return reply(route,{ok:true,items:ids.map(page_id=>({page_id,page_name:stale?'OLD RESPONSE':'Verified '+page_id,
        eligible_token_count:1,blocked_reason:'',authorization_pending:false,authorization_unknown:false,
        version:1,caption_template:'stale caption'}))}).catch(()=>{});
    }
    throw new Error('unexpected request '+url.pathname);
  });
  const settled=async count=>{
    await page.waitForFunction(()=>document.querySelectorAll('#pageRows tr').length===20);
    await page.waitForFunction(()=>document.querySelector('#pageAuthorizationNotice')?.textContent.startsWith('仅核验'));
    await page.waitForTimeout(150);
    assert(calls.length===count,`expected ${count} requests, got ${calls.length}`);
  };
  await page.goto('https://fb-list.test/pages.html');await settled(3);
  assert(lists===1,'list repeated');
  assert(calls.flat().join(',')===items.slice(0,20).map(p=>p.page_id).join(','),'first verification was not exactly current 20 rows');
  assert(await page.locator('#createPage').isEnabled(),'source busy disabled creation');
  assert(await page.locator('#excelPublishButton').isEnabled(),'source busy disabled Excel');
  assert((await page.locator('#pageRows').innerText()).includes('暂未核验'),'busy status missing');
  assert(await page.locator('#pageError').isHidden(),'source busy became a full-page error');
  await page.locator('#pageNext').click();await settled(6);
  assert(calls.slice(3).flat().join(',')===items.slice(20,40).map(p=>p.page_id).join(','),'next page queried wrong rows');
  await page.locator('#pagePrev').click();await settled(6);
  await page.locator('#reloadPages').click();await settled(9);
  mode='hold';await page.locator('#reloadPages').click();
  await page.waitForFunction(()=>document.querySelector('#pageAuthorizationNotice')?.textContent.includes('正在核验'));
  // The first old request is held while a newer generation completes.
  await page.waitForTimeout(100);assert(held,'stale fixture did not start');mode='ok';
  await page.locator('#reloadPages').click();await settled(13);
  releaseOld();await page.waitForTimeout(200);
  const text=await page.locator('#pageRows').innerText();
  assert(!text.includes('OLD RESPONSE'),'old generation overwrote current list');
  assert(text.includes('edited caption')&&text.includes('v7')&&!text.includes('stale caption'),'authorization replaced editable configuration');
  const summary={busy_list_retained:true,creation_enabled:true,excel_enabled:true,
    initial_rows:20,batches:[8,8,4],next_page:true,no_render_loop:true,refresh_retries:true,
    stale_response_ignored:true,config_merge_preserved:true,requests:calls.length};
  await page.goto('https://fb-list.test/publish.html');
  await page.locator('#applyPageFilters').click();
  const first=page.locator('#targetPages [data-page]').first();
  assert(await first.isEnabled(),'unknown authorization cannot enter preflight');
  assert((await page.locator('#targetPages').innerText()).includes('发布时核验'),'unknown authorization label missing');
  summary.unknown_selectable_for_live_preflight=true;
  return summary;
}
