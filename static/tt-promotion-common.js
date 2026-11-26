(function () {
  "use strict";
  const id = (key) => document.getElementById(key);
  const base = "/api/admin/tt-promotion";
  const usd = new Intl.NumberFormat("zh-CN", {style:"currency",currency:"USD"});
  const beijing = new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
  function h(value) {
    return String(value ?? "").replace(/[&<>"']/g,(c)=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  }
  function money(value) {return value == null || value === "" || !Number.isFinite(Number(value)) ? "待回传" : usd.format(Number(value));}
  function cents(value) {return value == null ? "待回传" : money(value / 100);}
  function time(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "—" : beijing.format(date);
  }
  function badge(text,style="") {return `<span class="pill ${h(style)}">${h(text)}</span>`;}
  function empty(columns,label) {return `<tr><td colspan="${columns}" class="empty">${h(label)}</td></tr>`;}
  function notify(message, inside=false, kind="success") {
    const target = inside && id("dialog").open && id("modal-error") ? id("modal-error") : id("notice");
    target.hidden = !message;
    target.dataset.kind = kind;
    target.setAttribute("role",kind==="error"?"alert":"status");
    target.textContent = message;
    if (message && inside) target.scrollIntoView({block:"nearest"});
  }
  function moduleRetry(node,label,retry) {
    node.replaceChildren();
    node.hidden=!label;
    if(!label)return;
    node.dataset.kind="error";
    node.setAttribute("role","alert");
    const button=document.createElement("button");
    button.type="button";button.className="button";button.textContent="重试";
    button.setAttribute("aria-label","重试 · "+label);
    node.append(document.createTextNode(label+" "),button);
    button.addEventListener("click",async()=>{
      button.disabled=true;
      try{await retry();}
      catch(e){moduleRetry(node,e.message||label,retry);}
      finally{button.disabled=false;}
    });
  }
  async function request(path,options) {
    const res = await fetch(path,{credentials:"same-origin",cache:"no-store",...options,headers:{Accept:"application/json",...(options?.body?{"Content-Type":"application/json"}:{})}});
    let body = {};
    try {body = await res.json();} catch {throw new Error(`服务未返回有效数据（HTTP ${res.status}）`);}
    if (!res.ok || body.ok === false) {
      const error = new Error(body.error?.message || body.message || (typeof body.error==="string"?body.error:null) || `请求失败（HTTP ${res.status}）`);
      error.status = res.status;
      throw error;
    }
    return body.data ?? body;
  }
  function get(path) {return request(base+path);}
  function post(path,payload) {return request(base+path,{method:"POST",body:JSON.stringify(payload)});}
  function modal(title,html) {
    id("dialog-title").textContent = title;
    id("dialog-content").innerHTML = `<div id="modal-error" role="alert" hidden></div>${html}`;
    if (!id("dialog").open) id("dialog").showModal();
    id("dialog").scrollTop = 0;
  }
  function confirmAction(title,message,verb) {
    const node = id("confirm");
    id("confirm-title").textContent = title;
    id("confirm-text").textContent = message;
    id("confirm-apply").textContent = verb;
    node.returnValue = "";
    node.showModal();
    return new Promise(resolve => {
      node.addEventListener("close",()=>resolve(node.returnValue==="apply"),{once:true});
      id("confirm-apply").onclick=()=>node.close("apply");
      id("confirm-cancel").onclick=()=>node.close("cancel");
      id("confirm-cancel").focus();
    });
  }
  function action(node,event,fn) {
    node.addEventListener(event, async (ev) => {
      const button = ev.target.closest?.("button");
      if (button?.dataset.busy) return;
      if (button) {button.dataset.busy="1";button.setAttribute("aria-busy","true");}
      try {await fn(ev);} catch(e) {notify(e.message || "操作失败",true,"error");}
      finally {if(button){delete button.dataset.busy;button.removeAttribute("aria-busy");}}
    });
  }
  async function boot(key,ready) {
    const nav = id("nav-toggle");
    nav.onclick=()=>{
      const expanded=nav.getAttribute("aria-expanded")!=="true";
      nav.setAttribute("aria-expanded",String(expanded));
      nav.textContent=expanded?"收起后台导航":"展开后台导航";
    };
    id("authButton").onclick=()=>location.assign(window.UiTopbar.loginUrl({}));
    let busy=false,scope="";
    async function reload(){
     if(busy)return;
     busy=true;
     try {
      const auth = await request("/api/ui/topbar");
      window.UiTopbar.render({auth,userCard:"#userCard",authButton:"#authButton"});
      id("authButton").onclick=()=>window.UiTopbar.handleAuthAction({auth,api:request});
      window.QuickNav.render({auth,container:"#quickNav",activeKey:key}).catch(()=>{});
      if (!auth.authenticated || !auth.user) {
        id("workspace").hidden=true;notify("请先登录 AI 自动后台。",false,"error");return;
      }
      const nextScope=JSON.stringify([auth.user.tenant_key,auth.user.user_id,auth.user.role,auth.user.permissions]);
      if(scope && nextScope!==scope){location.reload();return;}
      scope=nextScope;
      await ready(auth.user);
      if(id("notice").dataset.kind==="error")notify("");
      id("workspace").hidden=false;
    } catch(e) {
      if ([401,403].includes(e.status)) {
        id("workspace").hidden=true;
        if(id("dialog").open)id("dialog").close();
        notify(e.status===401?"登录已过期，请重新登录。":"当前账号没有访问此功能的权限。",false,"error");
      } else {
        moduleRetry(id("notice"),e.message||"页面数据读取失败",reload);
      }
      if(e.status===401){id("authButton").textContent="登录";id("authButton").onclick=()=>location.assign(window.UiTopbar.loginUrl({}));}
     } finally {busy=false;id("loading").hidden=true;}
    }
    id("refreshPage").onclick=reload;
    await reload();
  }
  function byId(row) {return String(row.account_id);}
  function name(row) {return row.nickname || row.account_display_name || row.account_username || byId(row);}
  function status(raw) {
    const states={needs_review:"待核对",enqueued:"已进入执行队列",missed:"已错过时段",selecting:"选材中",draft:"草稿",enabled:"已启用",disabled:"已停用",queued:"已入队",scheduled:"已预约",reserved:"已预约",ready:"就绪",eligible:"可选",selected:"已选材",matched:"已匹配",skipped:"已跳过",conflict:"时间冲突",blocked:"受限",preparing:"制作中",running:"执行中",pending:"待执行",waiting:"等待中",publishing:"提交中",reconciling:"回执核对中",unknown:"回执待核对",published:"已确认发布",confirmed:"已确认发布",failed:"失败",completed:"已完成",completed_with_errors:"部分失败",partial:"部分完成",cancelled:"已取消",canceled:"已取消",retry_wait:"等待重试",no_candidate:"无候选",manual:"手动",auto:"自动",scheduler:"排期",fixed:"固定",random:"随机",clear:"无冲突",preview:"预演",planned:"已计划",sent:"已提交"};
    return states[raw] || raw || "—";
  }
  function statusBadge(raw) {
    return badge(status(raw),["published","confirmed","completed","eligible","matched","ready","enabled","selected"].includes(raw)?"positive":["failed","unknown","blocked"].includes(raw)?"danger":["skipped","reconciling","conflict","partial","no_candidate","completed_with_errors"].includes(raw)?"warning":"");
  }
  const channelMetricFields=[
    {field:"revenue_28d",key:"",label:"28 天收入",unit:"USD"},
    {field:"revenue_total",key:"total_",label:"累计收入",unit:"USD"},
    {field:"followers",key:"followers_",label:"粉丝数",unit:"人"},
  ];
  function channelMetricRanges(value,row){
    return channelMetricFields.every(({field,key})=>{
      const min=value(key+"min"),max=value(key+"max"),actual=row[field];
      if(!min&&!max)return true;
      return actual!=null&&Number.isFinite(Number(actual))&&(!min||actual>=Number(min))&&(!max||actual<=Number(max));
    });
  }
  function channelMetricSort(order,a,b){
    const spec=channelMetricFields.find(({key})=>order===key+"asc"||order===key+"desc");
    if(!spec)return 0;
    const av=a[spec.field],bv=b[spec.field];
    if(av==null||bv==null)return av==null&&bv==null?0:av==null?1:-1;
    return (Number(av)-Number(bv))*(order===spec.key+"asc"?1:-1);
  }
  function channelRangeInputs(attribute){
    return channelMetricFields.map(({key,label,unit})=>
      `<fieldset class="metric-range"><legend>${label}（${unit}）</legend><div>${["min","max"].map((bound,i)=>
        `${i?'<span aria-hidden="true">—</span>':""}<input ${attribute}="${key+bound}" aria-label="${label}${i?"上限":"下限"}（${unit}）" type="number" ${unit==="人"?'min="0" step="1"':'step="0.01"'} placeholder="${i?"最高":"最低"}">`
      ).join("")}</div></fieldset>`
    ).join("");
  }
  function channelSortOptions(){
    return channelMetricFields.map(({key,label})=>`<option value="${key}desc">${label}降序</option><option value="${key}asc">${label}升序</option>`).join("");
  }
  function followersText(count){return count==null?"待回传":Number(count).toLocaleString("zh-CN");}
  window.TTPromotion={id,h,money,cents,time,badge,empty,notify,moduleRetry,get,post,modal,confirmAction,action,boot,byId,name,status,statusBadge,channelMetricRanges,channelMetricSort,channelRangeInputs,channelSortOptions,followersText};
})();
