/* playwright-cli run-code --filename tests/youtube_channel_templates_qa.js
 * All APIs are mocked; no production or platform writes. */
async (page) => {
  const checks=[], errors=[];
  const check=(name,ok)=>{if(!ok)throw new Error(name);checks.push(name);};
  const origin='http://127.0.0.1:8893';
  const reply=(route,value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
  const channel=(id,name,blocked=false)=>({id,name,channel_id:'UC'+id.repeat(22),channel_url:'https://www.youtube.com/channel/UC'+id.repeat(22),eligible:!blocked,auth_status:blocked?'blocked':'verified',comment_eligible:!blocked,reason:blocked?'授权已过期':'鉴权通过',checked_at:'2026-09-22T03:00:00Z'});
  const channels=[channel('1','DramaWave English'),channel('2','DramaWave Stories'),channel('3','待重新授权频道',true)];
  const empty=c=>({channel_id:c.channel_id,title_template:'',description_template:'',comment_template:'',version:0,updated_by:'',updated_by_name:'',updated_at:''});
  const templates=Object.fromEntries(channels.map(c=>[c.id,empty(c)]));
  templates['1'].description_template='频道描述 {desc}';
  let conflict=false, failRead=false, delay=false, held=null, submitted=null;
  const material={id:'1',name:'QA 素材',macro_name:'Example Drama',macro_desc:'Example synopsis',source_job_id:'a'.repeat(32),content_id:'123',language:'en',duration:'10:00',drama_cover_status:'available',drama_cover_url:origin+'/fixture.svg',thumbnail_url:origin+'/fixture.svg',url:'',size:'10MB'};
  await page.unrouteAll({behavior:'ignoreErrors'});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/fixture.svg',route=>route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><rect width="1280" height="720" fill="#dae4ff"/></svg>'}));
  await page.route('**/api/**',async route=>{
    const request=route.request(),path=request.url().replace(/^https?:\/\/[^/]+/,'').split('?')[0];
    if(path==='/api/ui/topbar')return reply(route,{authenticated:true,user:{name:'发布人',role:'user',is_admin:false,tenant_key:'qa',permissions:{youtube_auto_publish:true}}});
    if(path.endsWith('/channel-list'))return reply(route,{channels:channels.map(c=>({...c,template:{...templates[c.id],configured_fields:['title','description','comment'].filter(f=>templates[c.id][f+'_template'].trim())}})),checking:false});
    const match=path.match(/\/channels\/(\d+)\/template$/);
    if(match){
      const id=match[1];
      if(request.method()==='POST'){
        const data=request.postDataJSON();
        if(conflict)return reply(route,{message:'模板已被其他人修改，请重新加载'},409);
        templates[id]={...data,version:data.version+1,updated_by:'publisher',updated_by_name:'发布人',updated_at:'2026-09-22T03:10:00Z'};
        return reply(route,{template:templates[id]});
      }
      if(failRead)return reply(route,{message:'模板读取失败'},503);
      if(delay && id==='1'){held=route;return;}
      return reply(route,{template:templates[id]});
    }
    if(path.endsWith('/bootstrap'))return reply(route,{settings:{default_description:'默认描述'},source:{configured:true},channels_loaded:false,enabled:true,can_manage_settings:false});
    if(path.endsWith('/channels'))return reply(route,{channels,checking:false});
    if(path.endsWith('/materials'))return reply(route,{items:[material],configured:true,cache:{refreshing:false,stale:false},uploaders:[]});
    if(path.endsWith('/tasks')){
      if(request.method()==='POST'){submitted=request.postDataJSON();return reply(route,{task:{id:'a'.repeat(32),title:'Manual final',status:'queued_generation',material,channel:channels[1],versions:[],steps:[],created_at:'2026-09-22T03:00:00Z'}});}
      return reply(route,{items:[],counts:{all:0},total:0});
    }
    return reply(route,{error:'unexpected '+path},404);
  });
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(origin+'/youtube-channels.html');
  await page.locator('[data-edit="3"]').waitFor();
  check('all channels including blocked visible',await page.locator('[data-edit]').count()===3);
  check('channel navigation visible',await page.locator('#quickNav a[href="/youtube-channels.html"]').count()===1);
  await page.locator('#channel-search').fill('UC222');
  check('search matches channel ID',await page.locator('[data-edit]').count()===1);
  await page.locator('#channel-search').fill('');
  await page.locator('[data-edit="3"]').click();
  await page.locator('#template-title').waitFor();
  await page.waitForFunction(()=>!document.querySelector('#template-fields').disabled);
  await page.locator('#template-description').fill('Only description');
  await page.locator('#save-template').click();
  await page.waitForFunction(()=>!document.querySelector('#template-dialog').open);
  check('ordinary publisher saves blocked channel',templates['3'].description_template==='Only description');
  check('blank fields saved as blank',templates['3'].title_template===''&&templates['3'].comment_template==='');
  await page.locator('[data-edit="3"]').click();
  await page.waitForFunction(()=>!document.querySelector('#template-fields').disabled);
  await page.locator('#template-description').fill('Unsaved proposal'); conflict=true;
  await page.locator('#save-template').click();
  await page.waitForFunction(()=>document.querySelector('#template-message').textContent.includes('其他人'));
  check('conflict retains editor text',await page.locator('#template-description').inputValue()==='Unsaved proposal');
  conflict=false;await page.locator('#reload-template').click();
  await page.waitForFunction(()=>document.querySelector('#template-description').value==='Only description');
  await page.locator('#template-description').fill('');await page.locator('#save-template').click();
  await page.waitForFunction(()=>!document.querySelector('#template-dialog').open);
  check('clear all disables template',templates['3'].description_template==='');
  await page.screenshot({path:'output/playwright/youtube-channels-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  check('mobile page has no horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.locator('[data-edit="1"]').click();
  await page.waitForFunction(()=>!document.querySelector('#template-fields').disabled);
  await page.screenshot({path:'output/playwright/youtube-channel-template-mobile.png',fullPage:true});
  await page.locator('#close-template').click();
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(origin+'/youtube-publish.html');
  await page.locator('#new-publish').click();
  await page.waitForFunction(()=>!document.querySelector('#draft-channel').disabled);
  await page.locator('#draft-title').fill('Current title');await page.locator('#draft-comment').fill('Current comment');
  await page.locator('#draft-channel').selectOption('1');
  await page.waitForFunction(()=>document.querySelector('#draft-description').value==='频道描述 {desc}'&&!document.querySelector('#draft-title').disabled);
  check('description only leaves title',await page.locator('#draft-title').inputValue()==='Current title');
  check('description only leaves comment',await page.locator('#draft-comment').inputValue()==='Current comment');
  await page.locator('#draft-description').fill('Manual edit');
  await page.locator('[data-action="retry-channels"]').click();
  check('channel refresh preserves manual edits',await page.locator('#draft-description').inputValue()==='Manual edit');
  await page.locator('#draft-channel').selectOption('2');
  await page.waitForFunction(()=>document.querySelector('#modal-root').textContent.includes('该频道未配置模板'));
  check('empty next template keeps current values',await page.locator('#draft-description').inputValue()==='Manual edit');
  templates['1']={...templates['1'],title_template:'A title',description_template:'A description',comment_template:'A comment'};
  await page.locator('#draft-channel').selectOption('1');
  await page.waitForFunction(()=>document.querySelector('#draft-title').value==='A title');
  check('all nonempty fields apply',await page.locator('#draft-comment').inputValue()==='A comment');
  await page.locator('#draft-channel').selectOption('2');
  await page.waitForFunction(()=>document.querySelector('#modal-root').textContent.includes('该频道未配置模板'));
  check('switching retains previous channel title when blank',await page.locator('#draft-title').inputValue()==='A title');
  delay=true;await page.locator('#draft-channel').selectOption('1');
  await page.waitForFunction(()=>document.querySelector('#draft-title').disabled);
  check('loading blocks submission',await page.locator('[data-action="submit-publish"]').isDisabled());
  await page.locator('#draft-channel').selectOption('2');
  await page.waitForFunction(()=>!document.querySelector('#draft-title').disabled);
  await page.locator('#draft-title').fill('After switch');delay=false;
  if(held)await reply(held,{template:{...templates['1'],title_template:'STALE'}}).catch(()=>{});
  await page.waitForTimeout(100);
  check('late response cannot overwrite selected channel',await page.locator('#draft-title').inputValue()==='After switch');
  failRead=true;await page.locator('#draft-channel').selectOption('1');
  await page.locator('[data-action="retry-channel-template"]').waitFor();
  check('failed read keeps text and blocks submit',await page.locator('#draft-title').inputValue()==='After switch'&&await page.locator('[data-action="submit-publish"]').isDisabled());
  failRead=false;templates['1']={...templates['1'],title_template:' \n',description_template:'',comment_template:'\t'};
  await page.locator('[data-action="retry-channel-template"]').click();
  await page.waitForFunction(()=>document.querySelector('#modal-root').textContent.includes('该频道未配置模板'));
  check('whitespace fields do not override',await page.locator('#draft-title').inputValue()==='After switch');
  await page.locator('[data-action="choose-material"]').click();
  await page.locator('[data-action="select-material"]').click();await page.locator('[data-action="confirm-material"]').click();
  await page.locator('#draft-title').fill('Manual final');await page.locator('#draft-description').fill('Final description');await page.locator('#draft-comment').fill('Final comment');await page.locator('#draft-requirements').fill('16:9 cinematic cover');
  await page.screenshot({path:'output/playwright/youtube-template-publish.png',fullPage:true});
  await page.locator('[data-action="submit-publish"]').click();
  await page.waitForTimeout(200);
  check('submit uses final editable fields',submitted?.title_template==='Manual final'&&submitted.description_template==='Final description'&&submitted.comment_template==='Final comment');
  check('no browser runtime errors',errors.length===0);
  return {passed:checks.length,checks,errors};
}
