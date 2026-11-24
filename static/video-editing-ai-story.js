/* Read-only rough playback of the exact versioned source ranges submitted for export. */
(() => {
  "use strict";
  const stamp = (ms) => ms < 60000 ? (ms / 1000).toFixed(2) + " 秒"
    : Math.floor(ms / 60000) + " 分 " + ((ms % 60000) / 1000).toFixed(2) + " 秒";
  let generation = 0, timer, stopPlayer = () => {};
  function close() {
    generation++;
    clearTimeout(timer);
    stopPlayer();
    stopPlayer = () => {};
  }
  function orderText(session, mode) {
    let hint = session?.story_order_hint;
    if (!hint) {
      const selected = session?.selection?.items || session?.assets || [];
      const rows = selected.map(item => {
        const asset = item.asset || item;
        const episode = item.episode || asset.episode_number;
        const series = item.product && item.source_key ? JSON.stringify([item.product,item.source_key]) : asset.series_code;
        return {episode,series,label:episode ? "第 "+episode+" 集" : item.name || asset.name};
      });
      const verified = rows.length > 0 && rows.every(r=>r.series && r.series===rows[0].series && Number.isInteger(r.episode))
        && new Set(rows.map(r=>r.episode)).size === rows.length;
      hint = {verified,selection:rows.map(r=>r.label),episode:(verified?[...rows].sort((a,b)=>a.episode-b.episode):rows).map(r=>r.label)};
    }
    if (!hint.selection.length) return "选好素材后显示讲述顺序";
    const forward = mode === "episode" && hint.verified;
    const row = forward ? hint.episode : hint.selection;
    return "选入 " + hint.selection.join(" → ") + "　|　成片 " + row.join(" → ") +
      (mode === "episode" && !hint.verified && row.length > 1 ? "（集号或剧目无法统一核实，沿用选入顺序）" : "");
  }
  function mountPlayer(box, clips, assets, initial, status) {
    stopPlayer();
    const video = box.querySelector("video");
    video.preload = "auto";
    let index = initial, frame, sequence = 0, release = false;
    const assetMap = new Map(assets.map(a => [a.id, a]));
    const cancel = () => {
      if (frame != null && video.cancelVideoFrameCallback) video.cancelVideoFrameCallback(frame);
      frame = null;
    };
    const remove = () => {
      release = true; ++sequence; cancel();
      video.pause(); video.onloadedmetadata = video.ontimeupdate = video.onended = video.onerror = video.onseeked = null;
      video.removeAttribute("src");video.load();
    };
    stopPlayer = remove;
    function start(n) {
      if (release) return;
      ++sequence; const token = sequence;
      cancel(); video.pause(); index = n;
      box.querySelectorAll("[data-preview-segment]").forEach((b,i) => b.setAttribute("aria-current", String(i===n)));
      if (n >= clips.length) {
        status.textContent = "粗剪播放完毕"; return;
      }
      const c = clips[n], a = assetMap.get(c.source_id);
      if (!a) {status.textContent = "此段素材暂不可用，请重新打开方案";return;}
      const url = a.proxy_url || a.preview_url;
      status.textContent = (n+1) + " / " + clips.length + " · " + c.role + (a.proxy_url ? " · 流畅预览" : " · 原片预览");
      let armed = false;
      function check(t) {
        if (release || token !== sequence || !armed) return;
        if (t >= c.end_ms/1000 - .018) { start(index+1); return; }
        if (video.requestVideoFrameCallback) frame = video.requestVideoFrameCallback((_,meta)=>check(meta.mediaTime));
      }
      function play() {
        if (release || token!==sequence || armed) return;
        armed = true; video.onseeked = null;
        video.play().catch(()=>{if(token===sequence) status.textContent="点击播放器继续粗剪";});
        check(video.currentTime);
      }
      function seek() {
        if (release || token!==sequence) return;
        video.onloadedmetadata=null;
        video.onseeked=play;
        if (Math.abs(video.currentTime - c.start_ms/1000) < .015) play();
        else video.currentTime=c.start_ms/1000;
      }
      video.onended=()=>{if(armed && token===sequence)start(index+1);};
      video.ontimeupdate=()=>{if(!video.requestVideoFrameCallback)check(video.currentTime);};
      video.onerror=()=>{
        if (token!==sequence) return;
        if (a.proxy_url && video.getAttribute("src")===a.proxy_url && a.preview_url) {
          video.onloadedmetadata=seek;armed=false;video.src=a.preview_url;video.load();return;
        }
        status.textContent="此段暂时无法播放，请重试"; cancel();
      };
      if (video.getAttribute("src")===url && video.readyState>=1) seek();
      else {video.onloadedmetadata=seek;video.src=url;video.load();}
    }
    start(initial);
  }
  async function open(ctx) {
    close();
    const token=generation;
    const {api, esc, modal, get, act} = ctx;
    const original=get(), sid=original.id;
    modal("剪辑方案", "查看各集贡献与实际剪点", '<div id="storyPanel" aria-live="polite">正在读取剪辑方案…</div>', "", "story");
    const box = document.getElementById("storyPanel");
    const valid = () => token===generation && get()?.id===sid && box.isConnected && box.closest("dialog")?.open;
    const route = "/studio/sessions/"+sid;
    async function show(plan) {
      if (!valid()) return;
      let output=0;
      function draw() {
        stopPlayer();stopPlayer=()=>{};
        const o=plan.outputs[output];
        if (!o.segments.length) {
          box.innerHTML='<div class="story-heading"><strong>'+esc(o.question)+'</strong></div>'+
            plan.conflicts.map(w=>'<p class="story-note">'+esc(w)+'</p>').join('')+
            '<button class="primary" data-assets>补选连续集</button>';
          return;
        }
        const byId=new Map(get().assets.map(a=>[a.id,a]));
        box.innerHTML='<div class="story-heading"><strong>'+esc(o.question)+'</strong><small>方案 v'+plan.revision+' · '+stamp(o.duration_ms)+'</small></div>'+
          (plan.outputs.length>1?'<label>查看输出 <select data-story-output>'+plan.outputs.map((v,i)=>'<option value="'+i+'" '+(i===output?'selected':'')+'>'+esc(v.title)+'</option>').join('')+'</select></label>':'')+
          '<p>'+esc(orderText(get(),o.order_mode))+'</p><div class="story-coverage">'+o.coverage.map(c=>'<span>'+esc(c.episode?'第 '+c.episode+' 集':c.title)+' · '+c.segments+' 段 · '+stamp(c.duration_ms)+'</span>').join('')+'</div>'+
          [...plan.conflicts,...plan.warnings].map(w=>'<p class="story-note">'+esc(w)+'</p>').join('')+
          '<div class="story-layout"><div><video controls playsinline preload="none" aria-label="粗剪预览"></video><small data-status>按方案顺序播放原声片段</small></div><ol>'+
          o.segments.map((c,i)=>'<li><button type="button" data-preview-segment="'+i+'"><strong>'+esc(c.role)+'</strong> · '+esc(c.episode?'第 '+c.episode+' 集':byId.get(c.source_id)?.name||'素材')+' · '+stamp(c.start_ms)+' – '+stamp(c.end_ms)+'</button><p>'+esc(c.reason)+'</p>'+(c.transcript?'<small>'+esc(c.transcript)+'</small>':'')+'</li>').join('')+
          '</ol></div><div class="story-actions"><button data-play-story>连播粗剪</button><button data-change="opening_take">换开头</button><button data-change="ending_take">换结尾</button>'+
          (plan.conflicts.length?'<button class="primary" data-settings>调整剪辑设置</button>':'<button class="primary" data-submit-story>按此方案制作</button>')+'</div>';
        box.querySelector("[data-story-output]")?.addEventListener("change",e=>{output=Number(e.target.value);draw();});
      }
      draw();
      box.onclick=e=>{
        const b=e.target.closest("button");if(!b|| !valid())return;
        if(b.hasAttribute("data-play-story")||b.hasAttribute("data-preview-segment")){
          mountPlayer(box,plan.outputs[output].segments,get().assets,Number(b.dataset.previewSegment||0),box.querySelector("[data-status]"));return;
        }
        if(b.hasAttribute("data-assets")){close();return act(ctx.assets,"story-assets");}
        if(b.hasAttribute("data-settings")){close();return act(ctx.settings,"story-settings");}
        if(b.dataset.change)return act(async()=>{await ctx.change(b.dataset.change);if(valid())await open(ctx);},"story-change");
        if(b.hasAttribute("data-submit-story"))return act(()=>ctx.submit(plan.id),"story-submit");
      };
    }
    async function track(runId) {
      if (!valid())return;
      try {
        const s=(await api(route)).item;
        if(!valid())return;
        const r=s.runs.find(x=>x.id===runId);
        if(r?.status==="failed"||r?.status==="needs_attention"||r?.status==="superseded"){
          box.textContent=r.error||"素材选择已更新，请重新打开方案";
          if(r.status==="failed"){
            const retry=document.createElement("button");retry.textContent="重试方案";
            retry.onclick=()=>act(async()=>{await api("/studio/runs/"+runId+"/retry",{});await track(runId);},"story-retry");
            box.append(" ",retry);
          }
          return;
        }
        const pid=r?.edit_plan_id||s.edit_plan?.id;
        if(pid){
          if(s.edit_plan?.id!==pid){box.textContent="素材或剪辑配置已更新，请重新打开当前方案";return;}
          const p=(await api(route+"/edit-plans/"+pid)).item;if(valid()){await ctx.sync(s);await show(p);}return;
        }
        box.textContent=r?.status==="waiting_sources"?"等待所选素材准备，可关闭窗口，稍后查看。":"正在核对剧情和实际剪点，可关闭窗口，稍后查看。";
        timer=setTimeout(()=>track(runId),2000);
      } catch(e) {if(valid()){box.textContent=e.message+"，正在重新连接…";timer=setTimeout(()=>track(runId),4000);}}
    }
    try {
      if(original.edit_plan){return await show((await api(route+"/edit-plans/"+original.edit_plan.id)).item);}
      const r=await ctx.request(original);
      await track(r.run_id);
    } catch(e) {if(valid())box.textContent=e.message;}
  }
  window.AIEditorStory = {open, close, orderText};
})();
