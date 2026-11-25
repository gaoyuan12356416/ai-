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
  function notify(message, inside=false) {
    const target = inside && id("dialog").open && id("modal-error") ? id("modal-error") : id("notice");
    target.hidden = !message;
    target.textContent = message;
    if (message && inside) target.scrollIntoView({block:"nearest"});
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
      try {await fn(ev);} catch(e) {notify(e.message || "操作失败",true);}
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
    let auth;
    try {
      auth = await request("/api/ui/topbar");
      window.UiTopbar.render({auth,userCard:"#userCard",authButton:"#authButton",refreshButton:"#refreshPage"});
      id("authButton").onclick=()=>window.UiTopbar.handleAuthAction({auth,api:request});
      window.QuickNav.render({auth,container:"#quickNav",activeKey:key}).catch(()=>{});
      if (!auth.authenticated || !auth.user) {notify("请先登录 AI 自动后台。");return;}
      await ready(auth.user);
      id("workspace").hidden=false;
    } catch(e) {
      notify(e.status===401?"登录已过期，请重新登录。":e.status===403?"当前账号没有访问此功能的权限。":e.message);
      if(e.status===401){id("authButton").textContent="登录";id("authButton").onclick=()=>location.assign(window.UiTopbar.loginUrl({}));}
    } finally {id("loading").hidden=true;}
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
  window.TTPromotion={id,h,money,cents,time,badge,empty,notify,get,post,modal,confirmAction,action,boot,byId,name,status,statusBadge};
})();
