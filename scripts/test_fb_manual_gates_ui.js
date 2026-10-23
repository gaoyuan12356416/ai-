async (page) => {
  // Local mocked API only; no Facebook writes.
  await page.unroute('**/api/**');
  const checks=[], errors=[];
  const check=(v,n)=>{if(!v)throw new Error(n);checks.push(n);};
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(() => { window.confirm = () => true; });
  const source=[{page_id:'101',page_name:'Configured English',eligible_token_count:1},{page_id:'102',page_name:'Unconfigured',eligible_token_count:1}];
  let config={...source[0],config_instance_id:'a'.repeat(32),version:1,language:'en',caption_template:'Page default {name}',title:'Page title'};
  const material={material_id:'201',material_name:'Chinese material',language:'zh',drama_name:'',drama_description:'',duration_seconds:30};
  let last;
  await page.route('**/api/**',async route=>{
    const req=route.request(),u=new URL(req.url()),p=u.pathname.replace('/api/fb-video-publish',''),b=req.postDataJSON()||{};
    const reply=d=>route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true,...d})});
    if(u.pathname==='/api/ui/topbar')return reply({authenticated:true,user:{user_id:'fixture',tenant_key:'fixture',permissions:{fb_video_publish:true}}});
    if(p==='/languages')return reply({items:[{value:'en',label:'English'},{value:'zh',label:'Chinese'}]});
    if(p==='/pages'&&req.method()==='GET')return reply({items:u.searchParams.get('purpose')==='publish'?[config,{...source[1],config_instance_id:'',version:0,language:'',caption_template:'',title:''}]:[config]});
    if(p==='/pages/101'){config={...config,...b,version:config.version+1};return reply({items:[config]});}
    if(p==='/pages/available')return reply({items:source});
    if(p==='/materials')return reply({items:[material],has_more:false,next_cursor:''});
    if(p==='/draft')return reply({draft:null});
    if(p==='/preflight'){
      last=b;
      return reply({preview_token:'fixture',repeat_required:false,items:b.pairs.map(x=>({...x,page_name:source.find(y=>y.page_id===x.page_id).page_name,material_name:material.material_name,title:x.title||(x.page_id==='101'?config.title:''),caption:(x.caption_template||(x.page_id==='101'?config.caption_template:'')).replace('{name}',''),long_url_parameters:{},short_url:'[preview]',schedule_kind:'immediate'}))});
    }
    throw new Error('Unexpected route '+p);
  });
  await page.goto('http://127.0.0.1:18766/fb-pages.html');
  await page.locator('[data-edit-page="101"]').click();
  check(await page.locator('#editTitle').inputValue()==='Page title','saved Page title shown');
  await page.locator('#editTitle').fill('Updated Page title');
  await page.locator('#editCaption').fill('Default caption {name}');
  await page.locator('#savePage').click();
  await page.locator('#pageEditor').waitFor({state:'detached'});
  check(config.title==='Updated Page title','Page title persisted through save request');
  await page.goto('http://127.0.0.1:18766/fb-video-publish.html');
  await page.locator('#chooseMaterials').click();
  await page.locator('[data-material="201"]').check();
  await page.locator('#applyMaterials').click();
  await page.locator('#selectPages').click();
  await page.locator('#matchPairs').click();
  check(await page.locator('#pairRows input:checked').count()===1,'cross language configured Page paired; unconfigured Page excluded');
  await page.locator('#captionPair').selectOption('201::101');
  check(await page.locator('#pairCaption').inputValue()==='','blank caption inherits default');
  check(await page.locator('#pairTitle').getAttribute('placeholder')==='Updated Page title','title default visible');
  check((await page.locator('#captionPreview').innerText()).includes('Default caption'),'default caption rendered with missing metadata');
  await page.locator('#pairTitle').fill('Explicit title');
  await page.locator('#resetCaption').click();
  check(await page.locator('#pairTitle').inputValue()==='Explicit title','reset caption preserves independent title');
  await page.locator('#preflight').click();
  await page.locator('#publishConfirmation').waitFor();
  check(last.pairs.length===1&&last.pairs[0].page_id==='101','only managed Page reaches preflight');
  check(last.pairs[0].title==='Explicit title'&&last.pairs[0].caption_template==='','independent text fields reach preflight');
  check(errors.length===0,'no JavaScript page errors');
  return {checks};
}
