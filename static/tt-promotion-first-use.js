(function () {
  "use strict";
  const {h, name, byId, post} = window.TTPromotion;
  const levels = {PUBLIC_TO_EVERYONE:"所有人", MUTUAL_FOLLOW_FRIENDS:"互相关注的朋友", FOLLOWER_OF_CREATOR:"关注者", SELF_ONLY:"仅自己"};
  const needed = c => c.auth_eligible === true && c.policy_configured === false;

  // The same dialog is available in the channel directory and within a template editor.
  async function open(accounts) {
    const rows = accounts.filter(needed).map(channel=>({channel, options:[], choice:"", state:"loading", message:""}));
    if(!rows.length)return false;
    const dialog=document.createElement("dialog");
    dialog.className="first-use-dialog";
    dialog.setAttribute("aria-label","首次发布设置");
    dialog.innerHTML=`<div class="modal-head"><h2>确认首次发布的观看范围</h2><button type="button" class="button" data-close>关闭</button></div>
      <div class="first-use-body"><p>语言和参与状态已自动带入。按频道选择谁能观看，或将同一选项应用到支持它的频道。互动默认关闭。</p>
      <div class="inline" data-batch><label>批量观看范围 <select data-level></select></label><button type="button" class="button" data-apply>应用到支持的频道</button></div>
      <div data-rows></div>
      <fieldset class="group"><legend>本次确认频道的内容披露</legend>
      <div class="policy-checks"><label><input type="checkbox" data-brand> 推广自己的品牌或业务</label><label><input type="checkbox" data-partner> 推广第三方品牌或合作方</label><label><input type="checkbox" data-ai> 声明为 AI 生成或经 AI 显著处理的内容</label></div></fieldset>
      <div class="modal-foot"><span class="muted" data-progress role="status"></span><button type="button" class="button primary" data-save disabled>保存所选频道</button></div></div>`;
    document.body.append(dialog);
    const el=k=>dialog.querySelector("[data-"+k+"]");
    let saving=false,changed=false;
    const selectOptions=opts=>'<option value="">请选择</option>'+opts.map(v=>`<option value="${h(v)}">${h(levels[v])}</option>`).join("");
    function paint() {
      const batch=el("level"), current=batch.value;
      const valid=rows.filter(r=>r.state==="ready");
      const choices=Object.keys(levels).filter(key=>valid.some(r=>r.options.includes(key)));
      batch.innerHTML=selectOptions(choices);
      if(choices.includes(current))batch.value=current;
      el("batch").hidden=rows.length<2;
      el("apply").disabled=saving||!batch.value;
      el("rows").innerHTML=rows.map((r,i)=>`<div class="first-use-row"><div><strong>${h(r.creator||name(r.channel))}</strong><small class="secondary">#${h(byId(r.channel))}</small></div><div>${
        r.state==="ready"?`<select data-row="${i}" aria-label="${h(name(r.channel))}的观看范围">${selectOptions(r.options)}</select>`:
        h(r.state==="loading"?"正在读取平台选项…":r.state==="done"?r.message:"暂时无法确认")
      }${r.state==="error"?`<button class="row-link" type="button" data-retry="${i}">重试</button>`:""}
      ${r.message&&r.state!=="done"?`<small class="secondary">${h(r.message)}</small>`:""}</div></div>`).join("");
      for(const select of dialog.querySelectorAll("[data-row]"))select.value=rows[Number(select.dataset.row)].choice;
      const chosen=valid.filter(r=>r.choice && !(r.choice==="SELF_ONLY"&&el("partner").checked)).length;
      const finished=rows.filter(r=>r.state==="done").length;
      el("progress").textContent=`已确认 ${finished} / ${rows.length} 个频道 · 本次已选 ${chosen} 个`+(el("partner").checked&&valid.some(r=>r.choice==="SELF_ONLY")?" · 第三方品牌不可仅自己观看，该频道将跳过":"");
      el("save").disabled=saving||!chosen;
      for(const n of dialog.querySelectorAll("select,input,[data-close],[data-retry]"))n.disabled=saving;
    }
    async function read(r){
      r.state="loading";r.message="";
      try {
        const data=await post("/channels/"+encodeURIComponent(byId(r.channel))+"/creator-info",{});
        const info=data.creator_info;
        r.options=(info.privacy_level_options||[]).filter(k=>Object.hasOwn(levels,k));
        if(!r.options.length)throw new Error("TikTok 暂未返回可用观看范围");
        r.creator=info.creator_nickname;r.state="ready";
        if(!r.options.includes(r.choice))r.choice="";
      } catch(e){r.state="error";r.message=e.message;}
      if(dialog.open)paint();
    }
    async function each(target,callback){
      let next=0;
      async function consume(){while(dialog.open && next<target.length)await callback(target[next++]);}
      await Promise.all(Array.from({length:Math.min(target.length,3)},consume));
    }
    el("close").onclick=()=>dialog.close();
    dialog.oncancel=e=>{if(saving)e.preventDefault();};
    el("level").onchange=()=>{el("apply").disabled=!el("level").value;};
    el("apply").onclick=()=>{
      const value=el("level").value;
      for(const r of rows)if(r.state==="ready"&&r.options.includes(value))r.choice=value;
      paint();
    };
    el("rows").onchange=e=>{if(e.target.dataset.row!==undefined){rows[Number(e.target.dataset.row)].choice=e.target.value;paint();}};
    el("rows").onclick=e=>{const b=e.target.closest("[data-retry]");if(b&&!saving){const r=rows[Number(b.dataset.retry)];void read(r);paint();}};
    el("partner").onchange=paint;
    el("save").onclick=async()=>{
      if(saving)return;
      const flags={allow_comment:false,allow_duet:false,allow_stitch:false,brand_organic_toggle:el("brand").checked,
                   brand_content_toggle:el("partner").checked,is_aigc:el("ai").checked};
      const targets=rows.filter(r=>r.state==="ready"&&r.choice&&!(flags.brand_content_toggle&&r.choice==="SELF_ONLY"));
      saving=true;paint();
      try{
        await each(targets,async r=>{
          try {
            const result=await post("/channels/settings",{account_ids:[byId(r.channel)],first_use:true,settings:{publish_policy:{...flags,privacy_level:r.choice}}});
            changed=true;r.state="done";r.message=result.already_configured?"已沿用已有设置":"已保存 · "+levels[r.choice];
          } catch(e){r.state="error";r.message=e.message;}
          paint();
        });
      }finally{saving=false;paint();}
    };
    const closed=new Promise(resolve=>dialog.addEventListener("close",()=>{dialog.remove();resolve(changed);},{once:true}));
    dialog.showModal();paint();void each(rows,read);
    return closed;
  }
  window.TTFirstUse={needed,open};
})();
