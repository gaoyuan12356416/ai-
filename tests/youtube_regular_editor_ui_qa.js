/* Serve static/ at localhost:18964 and run with playwright-cli run-code --filename. */
async (page) => {
  await page.goto('about:blank');
  await page.unrouteAll({behavior:'wait'});
  const origin = 'http://127.0.0.1:18964';
  const sid = 'ss_' + '1'.repeat(32), tid = 'at_' + '2'.repeat(32);
  const ids = [1, 2, 8].map(n => 'aa_' + String(n).repeat(32));
  const assets = ids.map((id, i) => ({id, name: ['第1集','第2集','第8集'][i], duration_ms: 10000,
    series_code: '同一部剧', preview_url: origin + '/fake-media/' + id}));
  const original = {id:tid,name:'审核样片',status:'completed',version:1,original_task_id:tid,parent_task_id:'',
    unit_id:'youtube_regular',youtube_regular:true,created_at:123,
    output:{preview_url:origin+'/fake-media/final',metadata:{width:720,height:1280,duration_ms:15000}}};
  const session = {id:sid,version:1,title:'测试',source_ids:ids,assets,config:{},messages:[],runs:[]};
  const clips = [{id:'hook',source_id:ids[2],start_ms:1000,end_ms:2000},
    {id:'body_1',source_id:ids[0],start_ms:0,end_ms:10000},{id:'body_2',source_id:ids[1],start_ms:0,end_ms:10000}];
  const errors = [];
  const calls = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/quick-nav.js', r=>r.fulfill({contentType:'text/javascript',body:'window.QuickNav={render:async()=>{}}'}));
  await page.route('**/ui-topbar.js', r=>r.fulfill({contentType:'text/javascript',body:'window.UiTopbar={render:()=>({})}'}));
  await page.route('**/fake-media/**', r=>r.fulfill({status:204,body:''}));
  await page.route('**/video-editing-ai-youtube-material.js*', r=>r.fulfill({contentType:'text/javascript',
    body:'window.YoutubeMaterial={open:id=>{document.body.dataset.finalId=id}}'}));
  await page.route('**/api/**', async r=>{
    const req=r.request(), path=new URL(req.url()).pathname.replace('/api/ai-video-editing','');
    const body=req.postDataJSON()||{};
    const reply=(data)=>r.fulfill({json:data});
    if(path==='/api/ui/topbar') return reply({authenticated:true});
    if(path==='/studio/capabilities') return reply({});
    if(path==='/studio/sessions/'+sid){
      if(req.method()==='PATCH'){ session.config=body.config; session.version++; }
      return reply({item:session});
    }
    if(path==='/studio/sessions/'+sid+'/submit'){
      calls.push({kind:'submit',body});
      session.runs=[{id:'run1',status:'completed',kind:'create',tasks:[original],config:body.config}];
      return reply({run_id:'run1'});
    }
    if(path.startsWith('/studio/copies/'))return reply({items:{}});
    if(path==='/youtube-regular/tasks/'+tid+'/timeline'){
      if(req.method()==='GET')return reply({task:original,clips,sources:Object.fromEntries(assets.map(a=>[a.id,a]))});
      calls.push({kind:'refine',body});return reply({item:{id:'at_rev',version:2}});
    }
    return r.fulfill({status:404,json:{error:'unexpected '+path}});
  });
  await page.goto(origin + '/video-editing-ai.html#' + sid);
  await page.getByRole('navigation',{name:'创作工具'}).getByRole('button',{name:'YouTube 普通视频',exact:true}).click();
  const lastRow = page.locator('#regularSourceRows section').nth(2);
  await lastRow.getByLabel('正片',{exact:true}).uncheck();
  await lastRow.getByLabel('片头备选').check();
  await page.locator('#saveRegularConfig').click();
  await page.locator('#send').click();
  await page.locator('#regularTrim').waitFor({state:'visible'});
  const choice = calls[0].body.config.youtube_regular;
  if(JSON.stringify(choice.body_source_ids)!==JSON.stringify(ids.slice(0,2)) || choice.hook_source_ids[0]!==ids[2])
    throw Error('source roles changed');
  await page.getByText('这条成片会被 YouTube 归为 Shorts。',{exact:false}).waitFor();
  await page.locator('#regularTrim').click();
  await page.locator('[data-field=end_ms]').first().fill('4');
  await page.locator('[data-cmd=up]').nth(2).click();
  await page.locator('#submitRegularTrim').click();
  await page.locator('#regularClipRows').waitFor({state:'hidden'});
  const edited = calls[1].body.clips;
  if (edited[0].end_ms!==4000 || edited[1].id!=='body_2' || edited[2].id!=='body_1') throw Error('refinement lost');
  await page.locator('#regularIntake').click();
  if(await page.locator('body').getAttribute('data-final-id')!==tid) throw Error('incorrect selected final version');
  if(errors.length) throw Error(errors.join('; '));
  return {actualPage:true,bodySources:2,separateHook:8,editAndReorder:true,shortsWarning:true,frozenFinalId:true};
}
