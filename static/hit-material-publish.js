(() => {
  "use strict";
  const make = (tag, text) => { const n=document.createElement(tag); if(text)n.textContent=text; return n; };
  async function json(url, options={}) {
    const response=await fetch(url,{credentials:"same-origin",cache:"no-store",...options});
    const result=await response.json();
    if(!response.ok){const e=new Error(result.message||result.error_message||"请求未完成");e.code=result.code||result.error;throw e;}
    return result;
  }
  async function open(templateId) {
    if(document.getElementById("hitMaterialDialog"))return;
    const base="/api/admin/x-tag-auto-publish", dialog=make("dialog");dialog.id="hitMaterialDialog";
    dialog.style.width="min(680px, calc(100vw - 32px))";dialog.style.maxWidth="680px";
    const head=make("div"),title=make("h2","爆款素材发布");head.className="dialog-head";title.id="hitMaterialTitle";head.append(title);dialog.setAttribute("aria-labelledby",title.id);
    const body=make("div");body.className="dialog-body";body.style.display="grid";body.style.gap="12px";
    const scope=make("p","按语言、受众和题材收入画像匹配度一对一分配：每条素材只匹配一个账号，每个账号本次只发一条。");
    const tip=make("p","支持 1–200 条素材 ID。未到可投放时间的剧先制作，预约在可投放前 30 分钟发布；该时点已过则尽快发布。");
    const label=make("label","素材 ID"),input=make("textarea");input.id="hitMaterialInput";label.htmlFor=input.id;input.rows=6;input.maxLength=6400;input.placeholder="可用换行、逗号或空格分隔，重复 ID 自动合并";
    const status=make("p","正在读取模板…"),resultBox=make("div");status.setAttribute("role","status");status.style.whiteSpace="pre-wrap";
    body.append(scope,tip,label,input,status,resultBox);
    const foot=make("div"),close=make("button","关闭"),submit=make("button","确认发布");foot.className="dialog-foot";close.className="button";submit.className="button primary";submit.disabled=true;foot.append(close,submit);dialog.append(head,body,foot);document.body.append(dialog);dialog.showModal();
    let busy=false,done=false,draft=null,key="",version;
    close.onclick=()=>{if(!busy)dialog.close();};dialog.addEventListener("cancel",e=>{if(busy)e.preventDefault();});dialog.addEventListener("close",()=>dialog.remove());
    try {
      const [auth,data]=await Promise.all([json("/api/auth/status"),json(`${base}/templates/${templateId}`)]);
      if(!auth.authenticated||!auth.user?.user_id)throw Error("请先登录");
      const item=data.template||data.item||data;version=Number(item.version||item.current_version);
      if(!Number.isInteger(version)||version<=0)throw Error("模板版本读取失败");
      title.textContent="爆款素材发布 · "+(item.name||templateId);
      key=["x-hit-material-v1",auth.user.tenant_key,auth.user.user_id,templateId].join(":");
      const saved=sessionStorage.getItem(key);if(saved){draft=JSON.parse(saved);input.value=draft.material_ids.join("\n");input.readOnly=true;submit.textContent="重试确认";}
      status.textContent=draft?"存在尚未确认的提交，重试会核对同一轮任务。":"匹配不足的素材会显示原因，实际选材和制作进度可在发布记录中查看。";
      submit.disabled=false;input.focus();
    } catch(e){status.textContent=e.message;}
    submit.onclick=async()=>{
      if(busy||done)return;
      if(!draft){
        const ids=[...new Set(input.value.trim().split(/[\s,，;；]+/))];
        if(!ids.length||ids.length>200||ids.some(id=>!/^[1-9][0-9]{0,30}$/.test(id))){status.textContent="请输入 1–200 条有效数字素材 ID。";return;}
        draft={expected_version:version,material_ids:ids,idempotency_key:crypto.randomUUID().replaceAll("-",""),confirmed:true};
        try{sessionStorage.setItem(key,JSON.stringify(draft));}catch(_){draft=null;status.textContent="浏览器无法保存提交，请允许本站存储后重试。";return;}
      }
      busy=true;input.readOnly=true;submit.disabled=close.disabled=true;status.textContent="正在校验素材、匹配账号并创建任务…";
      try{
        const result=await json(`${base}/templates/${templateId}/run-now`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(draft)});
        if(result.ok!==true||!result.run_id)throw Error("未取得有效运行回执");
        done=true;submit.hidden=true;status.textContent=`运行 ${result.run_id} 已创建：匹配 ${result.matched_channels} 个账号，${(result.skipped_materials||[]).length} 条素材未匹配。`;
        for(const item of result.assignments||[]){const time=new Date(item.timing.publish_at_utc).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false});resultBox.append(make("p",`素材 ${item.material_id} → 账号 ${item.account_id} · 北京时间 ${time}`));}
        for(const item of result.skipped_materials||[])resultBox.append(make("p",`素材 ${item.material_id}：${item.reason}`));
        const link=make("a","查看发布记录");link.className="button";link.href="/x-tag-auto-publish-runs.html?run_id="+encodeURIComponent(result.run_id);resultBox.append(link);sessionStorage.removeItem(key);
      }catch(e){
        if(["invalid_request","hit_material_no_matching_channel","hit_material_operation_conflict","x_auto_template_version_conflict","x_auto_live_gates_closed"].includes(e.code)){draft=null;sessionStorage.removeItem(key);input.readOnly=false;submit.textContent="确认发布";status.textContent=e.message;}
        else{submit.textContent="重试确认";status.textContent=e.message+"。结果尚未确认，请重试核对同一轮任务。";}
      }finally{busy=false;submit.disabled=close.disabled=false;}
    };
  }
  window.HitMaterialPublish={open};
})();
