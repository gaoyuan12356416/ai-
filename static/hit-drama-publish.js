/* Shared, durable one-off drama request dialog for the two template lists. */
(() => {
  'use strict';
  function el(tag, text, cls) { const n=document.createElement(tag); if(text)n.textContent=text; if(cls)n.className=cls; return n; }
  async function json(url, options) {
    const response=await fetch(url,{credentials:'same-origin',cache:'no-store',...options});
    const result=await response.json();
    if(!response.ok) { const e=new Error(result.message||result.error_message||'请求未完成'); e.code=result.code||result.error; e.conflicts=Array.isArray(result.conflicts)?result.conflicts:[]; throw e; }
    return result;
  }
  async function open(platform, templateId) {
    if(document.getElementById('hitDramaDialog'))return;
    const isX=platform==='x', base=isX?'/api/admin/x-tag-auto-publish':'/api/admin/fb-auto-publish';
    const dialog=el('dialog'); dialog.id='hitDramaDialog'; dialog.setAttribute('aria-labelledby','hitDramaTitle');dialog.style.width='min(650px, calc(100vw - 32px))';dialog.style.maxWidth='650px';
    const title=el('h2','爆款剧发布'); title.id='hitDramaTitle'; const head=el('div','','dialog-head');head.append(title);
    const body=el('div','','dialog-body');body.style.display='grid';body.style.gap='12px';
    const scope=el('p',isX?'仅向模板中语言、受众方向和题材画像均匹配的频道各发布一次；无有效画像或不匹配的频道会跳过。':'仅向模板中语言、受众方向和题材画像均匹配的 Page 各发布一次；无有效画像或不匹配的 Page 会跳过。');
    const tip=el('p','沿用模板的素材、指标和冷却条件。未到可投放时间的剧先制作，预约在可投放前 30 分钟发布；该时点已过则尽快发布。本次操作不修改日常排期。');
    const label=el('label','ID 类型');label.htmlFor='hitDramaType';const type=el('select');type.id='hitDramaType';
    for(const [value,text] of [['content_id','剧 ID（单语言版本）'],['resource_id','资源 ID（关联各语言版本）']]){const option=el('option',text);option.value=value;type.append(option);}
    const idLabel=el('label','剧 ID 或资源 ID');idLabel.htmlFor='hitDramaInput';const input=el('input');input.id='hitDramaInput';input.type='text';input.maxLength=128;input.autocomplete='off';input.placeholder='输入剧 ID，例如 kOfK3rsZLN';
    function updateInput(){input.inputMode=type.value==='resource_id'?'numeric':'text';input.maxLength=type.value==='resource_id'?31:128;input.placeholder=type.value==='resource_id'?'输入资源 ID，例如 30911':'输入剧 ID，例如 kOfK3rsZLN';}
    type.addEventListener('change',updateInput);updateInput();
    const status=el('p','正在读取模板…');status.setAttribute('role','status');status.style.whiteSpace='pre-wrap';const resultBox=el('div');
    const typeField=el('div','','field'),idField=el('div','','field');typeField.append(label,type);idField.append(idLabel,input);
    body.append(scope,tip,typeField,idField,status,resultBox);
    const foot=el('div','','dialog-foot'),close=el('button','关闭','button'),submit=el('button','确认发布','button primary');close.type=submit.type='button';submit.disabled=true;foot.append(close,submit);
    dialog.append(head,body,foot);document.body.append(dialog);dialog.showModal();
    let busy=false,draft=null,key='',version=0,done=false;
    dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});dialog.addEventListener('close',()=>dialog.remove());close.onclick=()=>{if(!busy)dialog.close();};
    function frozen(value){type.disabled=value;input.readOnly=value;}
    function showFbSkipDetails(result){
      const reasons={page_not_publishable:'Page 当前不可发布',language_not_matched:'语言不匹配',audience_not_matched:'受众画像不匹配',theme_not_matched:'题材画像不匹配',candidate_metadata_missing:'剧目分类缺失',candidate_metadata_unavailable:'剧目分类暂不可用',no_revenue_history:'暂无收入画像',no_positive_revenue:'暂无正收入画像',no_valid_audience_profile:'暂无有效受众画像',no_valid_theme_profile:'暂无有效题材画像',portrait_unavailable:'画像暂不可用',portrait_incomplete:'画像数据不完整',portrait_invalid:'画像数据异常',portrait_mapping_outdated:'画像版本待更新',portrait_hierarchy_outdated:'画像版本待更新',signed_income_adjustment:'收入含冲正，暂无可用偏好',unsupported_profile_product:'当前产品没有可用画像',fb_auto_page_language_missing:'Page 未配置有效语言',fb_page_missing_eligible_token:'Page 没有可用发布授权',fb_auto_no_eligible_video:'模板来源、上线时间、指标或时长条件下没有合格素材',fb_auto_no_video_after_drama_cooldown:'该剧仍在同剧冷却期',fb_auto_page_frequency_limit:'Page 本时段发布次数已达限制',fb_auto_page_task_conflict:'Page 当前任务冲突',fb_auto_material_cooldown:'素材仍在冷却期'};
      reasons.fb_auto_no_video_after_material_cooldown='素材仍在冷却期或已被其他任务占用';
      if(result.match_policy==='legacy_language_only')reasons.fb_auto_no_video_after_drama_cooldown='旧记录：冷却或无合格素材，需核对实际原因';
      const reasonText=reason=>reasons[reason]||`未满足发布条件（${reason||'原因待核对'}）`;
      if(Array.isArray(result.skip_summary)&&result.skip_summary.length){
        const details=el('details'),summary=el('summary','查看跳过原因汇总');details.append(summary);
        for(const item of result.skip_summary)details.append(el('p',`${reasonText(item.reason)}：${item.count} 个 Page`));
        resultBox.append(details);
      }
      if(Array.isArray(result.skipped_pages)&&result.skipped_pages.length){
        const details=el('details'),summary=el('summary','查看跳过的 Page');details.append(summary);
        for(const item of result.skipped_pages)details.append(el('p',`Page ${item.page_id}：${reasonText(item.reason)}`));
        resultBox.append(details);
      }
    }
    function showResult(result){
      done=true;submit.disabled=true;submit.remove();status.textContent=isX?`已创建运行 ${result.run_id}：匹配 ${result.matched_channels} 个频道，跳过 ${(result.skipped_channels||[]).length} 个频道。实际选材和发布进度请查看记录。`:`已创建运行 ${result.run_id}：已排队 ${result.queued} 个 Page，跳过 ${result.skipped} 个 Page。`;
      if(!isX){
        const matched=Number.isInteger(result.matched_pages)?`画像匹配 ${result.matched_pages} 个 Page。`:'';
        const skipped=Number(result.skipped||0)+(Array.isArray(result.skipped_pages)?result.skipped_pages.length:0);
        status.textContent=Number(result.queued)===0?`运行 ${result.run_id}：本轮未发布。${matched}已排队 0 个 Page，共跳过 ${skipped} 个 Page。`:`已创建运行 ${result.run_id}：${matched}已排队 ${result.queued} 个 Page，共跳过 ${skipped} 个 Page。实际发布进度请查看记录。`;
        if(result.match_policy==='legacy_language_only')status.textContent+=' 本条为改造前的提交回执，当时未按画像筛选 Page。';
      }
      if(result.languages?.length)status.textContent=`已识别 ${result.languages.length} 种语言版本。`+status.textContent;
      const link=el('a','查看发布记录','button');link.href=(isX?'/x-tag-auto-publish-runs.html':'/fb-auto-publish-runs.html')+'?run_id='+encodeURIComponent(result.run_id);resultBox.append(link);
      if(result.skipped_channels?.length){const details=el('details'),summary=el('summary','查看跳过的频道');details.append(summary);const reasons={account_not_publishable:'账号当前不可发布',language_not_matched:'语言不匹配',audience_not_matched:'受众画像不匹配',theme_not_matched:'题材画像不匹配',candidate_metadata_missing:'剧目分类缺失',no_revenue_history:'暂无收入画像',no_positive_revenue:'暂无正收入画像',no_valid_audience_profile:'暂无有效受众画像',portrait_unavailable:'画像暂不可用',portrait_incomplete:'画像数据不完整',portrait_mapping_outdated:'画像版本待更新',portrait_hierarchy_outdated:'画像版本待更新'};for(const item of result.skipped_channels)details.append(el('p',`频道 ${item.account_id}：${reasons[item.reason]||'画像暂不满足匹配条件'}`));resultBox.append(details);}
      if(!isX)showFbSkipDetails(result);
      try{sessionStorage.removeItem(key);}catch(_){}
    }
    try{
      const [auth,data]=await Promise.all([json('/api/auth/status'),json(`${base}/templates/${templateId}`)]);
      if(!auth.authenticated||!auth.user?.user_id)throw Error('请先登录');
      const item=data.template||data.item||data;version=Number(item.version||item.current_version);
      if(!Number.isInteger(version)||version<=0)throw Error('模板版本读取失败，请刷新后重试');
      title.textContent='爆款剧发布 · '+(item.name||`模板 ${templateId}`);
      key=['hit-drama-v1',platform,auth.user.tenant_key,auth.user.user_id,templateId].join(':');
      const saved=sessionStorage.getItem(key);if(saved){draft=JSON.parse(saved);type.value=draft.id_type;updateInput();input.value=draft.drama_id;frozen(true);submit.textContent='重试确认';status.textContent='存在尚未确认的提交。重试将查询或继续同一轮任务。';}else status.textContent='资源 ID 会关联各语言剧目，再按发布目标的语言选材。';
      submit.disabled=false;input.focus();
    }catch(error){status.textContent=error.message;}
    submit.onclick=async()=>{
      if(busy||done)return;
      if(!draft){
        const id=input.value.trim(),pattern=type.value==='resource_id'?/^[1-9][0-9]{0,30}$/:/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
        if(!pattern.test(id)){status.textContent=type.value==='resource_id'?'资源 ID 请输入正整数。':'剧 ID 支持字母、数字、下划线和短横线。';input.focus();return;}
        const operation=crypto.randomUUID().replaceAll('-','');
        draft={expected_version:version,drama_id:id,id_type:type.value,...(isX?{idempotency_key:operation,confirmed:true}:{operation_id:operation})};
        try{sessionStorage.setItem(key,JSON.stringify(draft));}catch(_){draft=null;status.textContent='浏览器无法保存本次操作，请允许本站存储后重试。';return;}
      }
      if(!isX)resultBox.textContent='';
      busy=true;submit.disabled=close.disabled=true;frozen(true);status.textContent='正在解析剧目、筛选发布范围并创建任务…';
      try{const result=await json(`${base}/templates/${templateId}/run-now`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(draft)});if(result.ok!==true||!result.run_id)throw Error('未取得有效运行回执');showResult(result);}
      catch(error){
        const rejected=new Set(['hit_drama_not_found','hit_drama_no_matching_channel','hit_drama_operation_conflict','x_auto_template_version_conflict','fb_auto_version_conflict','x_auto_live_gates_closed','fb_auto_live_gate_closed','fb_auto_prebuild_gate_closed']);
        if(!isX)rejected.add('hit_drama_no_matching_page');
        if(rejected.has(error.code)){status.textContent=error.message;draft=null;sessionStorage.removeItem(key);frozen(false);submit.textContent='确认发布';if(!isX&&error.code==='hit_drama_no_matching_page')showFbSkipDetails({skipped_pages:error.conflicts});}
        else{status.textContent=`${error.message}。提交结果尚未确认，请点“重试确认”核对同一轮任务。`;submit.textContent='重试确认';}
      }finally{busy=false;submit.disabled=done;close.disabled=false;}
    };
  }
  window.HitDramaPublish={open};
})();
