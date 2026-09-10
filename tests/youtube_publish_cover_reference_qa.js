/* Run via playwright-cli run-code --filename after opening the local static server.
 * Every API/image response is isolated; no production generation, notification, or upload. */
async (page) => {
  const origin = page.url().match(/^http:\/\/127\.0\.0\.1:\d+/)?.[0] || 'http://127.0.0.1:8878';
  const checks = [], calls = [], errors = [], imageRequests = [];
  const check = (name, valid) => { if (!valid) throw new Error(name); checks.push(name); };
  const reply = (route, data, status = 200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  const videoCover = origin + '/qa-video-thumbnail.svg', dramaCover = origin + '/qa-drama-poster.svg';
  const frozenCover = '/api/youtube-auto-publish/reference-covers/' + 'f'.repeat(32);
  const generatedCover = origin + '/qa-generated-cover.svg';
  const fixture = {name:'manual-cover.png',mimeType:'image/png',buffer:await page.screenshot({type:'png'})};
  const channel = {id:'channel-1',name:'DramaWave English',language:'en',eligible:true,comment_eligible:true};
  const base = {name:'发布视频素材.mp4',macro_name:'The Secret Heiress',macro_desc:'A story about family and a hidden identity.',macro_url:'',long_url:'https://example.invalid/qa/drama',content_id:'drama-1',link_ready:true,url:'',thumbnail_url:videoCover,language:'en',duration:'12:30',size:'80 MB'};
  const materials = [
    {...base,id:'available',drama_cover_status:'available',drama_cover_url:dramaCover},
    {...base,id:'missing',name:'缺少原剧封面素材',drama_cover_status:'missing',drama_cover_url:'',drama_cover_message:'所属剧暂未配置原剧封面。'},
    {...base,id:'ambiguous',name:'剧集关联不唯一素材',drama_cover_status:'ambiguous',drama_cover_url:dramaCover,drama_cover_message:'匹配到多个剧集，请先核对所属剧。'},
    {...base,id:'invalid',name:'无效原剧封面素材',drama_cover_status:'invalid',drama_cover_url:'javascript:window.coverInjection=1',drama_cover_message:'原剧封面地址不合法。'},
    {...base,id:'unsafe',name:'无效协议素材',drama_cover_status:'available',drama_cover_url:'javascript:window.coverInjection=1'},
    {...base,id:'legacy',name:'历史素材，无封面元数据'},
    {...base,id:'escaped',macro_name:'<img src=x onerror="window.coverInjection=1">',drama_cover_status:'missing',drama_cover_message:'<script>window.coverInjection=1</script>'}
  ];
  const reference = {url:frozenCover,drama_name:'The Secret Heiress',frozen:true};
  const makeTask = (id,extra={}) => ({id,title:'The Secret Heiress | Official story',title_template:'{name} | Official story',description:'A story about family.',description_template:'{desc}',comment:'',comment_template:'',material:materials[0],channel,cover_source:'ai',requirements:'保留原剧主要人物，以蓝金色电影风格突出人物关系，构图适合16:9视频封面。',reference_cover:reference,status:'review',phase:'cover',created_at:'2026-09-10T09:00:00Z',cover_url:generatedCover,current_version:1,versions:[{number:1,url:generatedCover,feedback:'首次生成',created_at:'2026-09-10T09:00:00Z'}],steps:[],notification:{status:'sent',message:'已发送审核提醒'},can_review:true,can_retry:false,...extra});
  let created = 0;
  const tasks = [makeTask('review-reference'),makeTask('legacy-task',{reference_cover:null})];
  const svg = (kind) => kind === 'video' ? '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><rect width="1280" height="720" fill="#e0e8f4"/><text x="90" y="390" fill="#344c6e" font-size="74">Video material thumbnail</text></svg>' : kind === 'poster' ? '<svg xmlns="http://www.w3.org/2000/svg" width="540" height="720"><defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="#1d3359"/><stop offset="1" stop-color="#586b87"/></linearGradient></defs><rect width="540" height="720" fill="url(#g)"/><circle cx="270" cy="250" r="100" fill="#b9c8df"/><path d="M90 550Q270 300 450 550" fill="#819abc"/><text x="45" y="635" fill="#fff" font-size="30">THE SECRET HEIRESS</text><text x="130" y="682" fill="#d8e0ec" font-size="22">REFERENCE · QA</text></svg>' : '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"><defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="#102844"/><stop offset="1" stop-color="#657b9c"/></linearGradient></defs><rect width="1280" height="720" fill="url(#g)"/><circle cx="960" cy="250" r="130" fill="#c8d4e5"/><path d="M680 720Q960 230 1240 720" fill="#839cb9"/><text x="65" y="275" fill="#f2dbac" font-size="64">THE SECRET</text><text x="65" y="370" fill="#f2dbac" font-size="86">HEIRESS</text><text x="70" y="520" fill="#d8e3f5" font-size="28">16:9 generated cover · QA</text></svg>';
  page.on('pageerror', e => errors.push(e.message));
  await page.unrouteAll({behavior:'ignoreErrors'});
  await page.route('**/qa-*.svg', route => { const url=route.request().url();imageRequests.push(url);return route.fulfill({status:200,contentType:'image/svg+xml',body:svg(url.includes('thumbnail')?'video':url.includes('poster')?'poster':'generated')}); });
  await page.route('**/navigation.json', route => reply(route,[{key:'youtube',label:'YouTube 社媒',items:[{key:'youtubeAutoPublish',label:'YouTube 自动发布',kind:'page',href:'/youtube-publish.html',module:'youtube_auto_publish',enabled:true}]}]));
  await page.route('**/api/**', route => {
    const req=route.request(), path=req.url().replace(/^https?:\/\/[^/]+/,'').split('?')[0], body=req.postDataJSON();
    calls.push({path,method:req.method(),body});
    if(path===frozenCover){imageRequests.push(req.url());return route.fulfill({status:200,contentType:'image/svg+xml',body:svg('poster')});}
    if(path==='/api/ui/topbar')return reply(route,{authenticated:true,user:{name:'封面参考 QA',role:'admin',is_admin:true,tenant_key:'qa',permissions:{youtube_auto_publish:true}}});
    if(path.endsWith('/bootstrap'))return reply(route,{settings:{default_description:'{desc}'},source:{configured:true},enabled:true,channels:[],channels_loaded:false,can_manage_settings:true});
    if(path.endsWith('/channels'))return reply(route,{channels:[channel]});
    if(path.endsWith('/materials'))return reply(route,{configured:true,items:materials});
    if(path.endsWith('/covers'))return reply(route,{asset:{id:'manual-cover-asset',url:generatedCover}});
    if(path.endsWith('/tasks')&&req.method()==='GET')return reply(route,{items:tasks,total:tasks.length,counts:{all:tasks.length,review:tasks.filter(t=>t.can_review).length}});
    if(path.endsWith('/tasks')&&req.method()==='POST'){
      const material=materials.find(m=>m.id===body.material_id);
      const t=makeTask('created-'+(++created),{material,cover_source:body.cover_source,reference_cover:body.cover_source==='ai'?reference:null,status:body.cover_source==='ai'?'generating':'uploading',can_review:false});tasks.unshift(t);return reply(route,{task:t});
    }
    const match=path.match(/\/tasks\/([^/]+)(?:\/(review))?$/);
    if(match){const t=tasks.find(t=>t.id===match[1]);if(!t)return reply(route,{error:'unknown task'},404);if(match[2]){t.status=body.action==='reject'?'generating':'uploading';t.can_review=false;}return reply(route,{task:t});}
    return reply(route,{error:'unexpected route '+path},404);
  });
  await page.setViewportSize({width:1440,height:1100});
  await page.goto(origin+'/youtube-publish.html');
  await page.locator('#page-content:not(.hidden)').waitFor();
  await page.locator('#new-publish').click();
  check('unselected material prompts original cover selection',await page.locator('.drama-reference-empty').innerText().then(t=>t.includes('选择视频素材后')));
  const choose = async id => { await page.locator('[data-action="choose-material"]').click();await page.locator('[data-action="select-material"][data-id="'+id+'"]').click();await page.locator('[data-action="confirm-material"]').click(); };
  await choose('available');
  await page.locator('#draft-channel').selectOption(channel.id);
  await page.locator('#draft-requirements').fill('保留原剧主要人物，以蓝金色电影风格突出人物关系，构图适合16:9视频封面。');
  check('actual drama name shown separately from filename',await page.locator('.drama-reference-name').innerText()==='The Secret Heiress');
  check('AI reference uses original drama poster',await page.locator('.drama-reference-image img').getAttribute('src')===dramaCover);
  check('material continues using video thumbnail',await page.locator('.selection-summary .mini-cover').getAttribute('src')===videoCover);
  check('reference generation explanation shown',await page.locator('.drama-reference-copy').innerText().then(t=>t.includes('将以原剧封面为参考，结合封面要求生成16:9图片')));
  await page.locator('.drama-reference-card').scrollIntoViewIfNeeded();
  await page.screenshot({path:'output/playwright/youtube-cover-reference/01-ai-reference-1440.png',fullPage:true,animations:'disabled'});
  const before=calls.filter(c=>c.method==='POST'&&c.path.endsWith('/tasks')).length;
  for(const id of ['missing','ambiguous','invalid','unsafe','legacy']){
    await choose(id);
    const material=materials.find(m=>m.id===id);
    check(id+' has no substitute reference image',await page.locator('.drama-reference-image').count()===0);
    check(id+' has actionable local upload alternative',await page.locator('.drama-reference-empty').innerText().then(t=>t.includes('本地上传')));
    if(material.drama_cover_message)check(id+' retains exact backend reason',await page.locator('.drama-reference-empty').innerText().then(t=>t.includes(material.drama_cover_message)));
    await page.locator('[data-action="submit-publish"]').click();
    check(id+' blocks AI POST',calls.filter(c=>c.method==='POST'&&c.path.endsWith('/tasks')).length===before&&await page.locator('.error-message').count()>0);
  }
  await choose('escaped');
  check('reference messages escaped',await page.locator('.drama-reference-empty').innerText().then(t=>t.includes('<script>'))&&await page.evaluate(()=>!window.coverInjection));
  await choose('missing');
  await page.locator('.drama-reference-empty').scrollIntoViewIfNeeded();
  await page.screenshot({path:'output/playwright/youtube-cover-reference/02-reference-missing-1440.png',fullPage:true,animations:'disabled'});
  await page.locator('[data-action="cover-source"][data-id="local"]').click();
  check('local cover hides AI reference gate',await page.locator('.drama-reference-empty,.drama-reference-card').count()===0);
  await page.locator('#draft-cover-file').setInputFiles(fixture);
  await page.locator('.upload-preview-row img').waitFor();
  await page.locator('[data-action="submit-publish"]').click();
  await page.getByRole('heading',{name:'发布任务详情',exact:true}).waitFor();
  check('local upload succeeds without original drama cover',calls.some(c=>c.body?.cover_source==='local'&&c.body.material_id==='missing'&&c.body.cover_asset_id==='manual-cover-asset'));
  check('local detail invents no reference',await page.locator('.drama-reference-card').count()===0);
  await page.locator('.close-modal').click();
  await page.locator('#new-publish').click();await choose('available');await page.locator('#draft-channel').selectOption(channel.id);await page.locator('#draft-requirements').fill('使用原剧人物制作横版封面');
  await page.locator('[data-action="submit-publish"]').click();await page.getByRole('heading',{name:'发布任务详情',exact:true}).waitFor();
  const aiCall=calls.find(c=>c.body?.cover_source==='ai');
  check('AI submit uses selected material and requirements',aiCall?.body.material_id==='available'&&aiCall.body.requirements==='使用原剧人物制作横版封面');
  check('client does not submit editable reference URL',aiCall&&!('reference_cover' in aiCall.body)&&!('drama_cover_url' in aiCall.body));
  check('task detail uses frozen private reference',await page.locator('.drama-reference-image img').getAttribute('src')===origin+frozenCover);
  check('task marks frozen reference original',await page.locator('.drama-reference-copy').innerText().then(t=>t.includes('已固定为本任务的参考原图')));
  await page.locator('.drama-reference-card').scrollIntoViewIfNeeded();
  await page.screenshot({path:'output/playwright/youtube-cover-reference/03-detail-reference-1440.png',fullPage:true,animations:'disabled'});
  await page.locator('.close-modal').click();
  await page.locator('[data-action="review"][data-id="review-reference"]').click();
  check('review final and original cover differ',await page.locator('.review-image>img').getAttribute('src')===generatedCover&&await page.locator('.drama-reference-image img').getAttribute('src')===origin+frozenCover);
  check('frozen drama name shown in review',await page.locator('.drama-reference-name').innerText()==='The Secret Heiress');
  check('all review actions retained',await page.locator('[data-action="approve"],[data-action="reject"],[data-action="manual"]').count()===3);
  for(const width of [1366,1920]){await page.setViewportSize({width,height:1000});check('reference review no overflow at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
  await page.setViewportSize({width:1440,height:1000});
  await page.screenshot({path:'output/playwright/youtube-cover-reference/04-review-reference-1440.png',fullPage:true,animations:'disabled'});
  await page.locator('[data-action="reject"]').click();await page.locator('#reject-reason').fill('保留参考封面中的角色服装');await page.locator('[data-action="confirm-reject"]').click();await page.getByRole('heading',{name:'发布任务详情',exact:true}).waitFor();
  check('reject retains frozen reference',await page.locator('.drama-reference-image img').getAttribute('src')===origin+frozenCover&&calls.some(c=>c.body?.action==='reject'&&c.body.feedback==='保留参考封面中的角色服装'));
  await page.locator('.close-modal').click();
  await page.locator('[data-action="review"][data-id="legacy-task"]').click();
  check('historical review without reference has no invented poster',await page.locator('.drama-reference-card').count()===0);
  check('historical review remains approvable',!(await page.locator('[data-action="approve"]').isDisabled()));
  await page.locator('[data-action="approve"]').click();await page.getByRole('heading',{name:'发布任务详情',exact:true}).waitFor();
  check('historical detail without reference remains clean',await page.locator('.drama-reference-card').count()===0);
  check('private reference image requested from current origin',imageRequests.includes(origin+frozenCover));
  check('production has no simulation controls',await page.getByRole('button',{name:/模拟飞书提醒|重置演示/}).count()===0);
  check('no JavaScript errors',errors.length===0);
  return {passed:checks.length,checks,errors,screenshots:4};
}
