/* Independent editing studio. Only authentication is supplied by the host. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id),
    API = "/api/ai-video-editing";
  const esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const clone = (v) => JSON.parse(JSON.stringify(v)),
    op = () => crypto.randomUUID();
  const methods = {
    hook_first: ["热点前置", "精彩开头后完整连播"],
    highlight_montage: ["高燃混剪", "串联亮点，紧凑呈现"],
    golden_three: ["黄金三段", "开场 · 剧情 · 悬念"],
    cliffhanger: ["爆点引流", "围绕一个剧情问题，在答案揭晓前收住"],
  };
  const effects = {
    basic: "基础调整",
    mirror: "水平镜像",
    zoom: "画面缩放",
    tilt: "角度倾斜",
    border: "画面边框",
    vignette: "暗角效果",
    ghost: "叠影",
    frame_drop: "抽帧",
    picture_in_picture: "画中画",
  };
  const icons = {
    history: "M4 5v5h5 M4 10a8 8 0 1 1 0 5 M12 8v5l3 2",
    works: "M5 3h11l4 4v14H5z M15 3v5h5 M8 12h9 M8 16h6",
    media: "M3 5h18v14H3z M3 9h18 M7 5v4 M17 5v4 M10 12l5 3-5 2z",
    timeline: "M4 5h16 M4 12h16 M4 19h16 M8 2v6 M15 9v6 M10 16v6",
    scissors:
      "M8 8l12 12 M8 16L20 4 M8 6a3 3 0 1 1-6 0 3 3 0 0 1 6 0 M8 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
    mic: "M9 4a3 3 0 0 1 6 0v8a3 3 0 0 1-6 0z M5 10v2a7 7 0 0 0 14 0v-2 M12 19v3 M8 22h8",
    translate:
      "M3 5h12 M9 2v3 M6 5c0 6 4 9 8 10 M12 5c0 6-4 9-9 10 M14 21l4-11 4 11 M16 17h4",
    send: "M3 3l18 9-18 9 4-9z M7 12h14",
    download: "M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5",
    plus: "M12 4v16 M4 12h16",
  };
  const svg = (k) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${icons[k] || icons.media}"/></svg>`;
  const iconify = (root) =>
    root.querySelectorAll("[data-icon]").forEach((e) => {
      if (!e.querySelector("svg"))
        e.insertAdjacentHTML("afterbegin", svg(e.dataset.icon));
    });
  const time = (ms) => {
    let s = Math.max(0, Math.floor((ms || 0) / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  const labels = {
    queued: "等待处理",
    waiting_sources: "等待所选素材准备",
    needs_attention: "需要处理缺失素材",
    superseded: "已按新选择继续",
    analyzing: "正在解析素材",
    preparing: "正在生成脚本与字幕",
    rendering: "正在制作成片",
    running: "制作中",
    completed: "已完成",
    failed: "制作失败",
    succeeded: "解析完成",
  };
  const Sources = window.AIEditorSourceState;
  const Production = window.AIEditorProduction;
  let lastSyncedAt = 0, worksTimer, worksEpoch = 0, worksFailures = 0;
  const actionLocks = new Map();
  let saveChain = Promise.resolve(),
    sessionPromise = null,
    navigationEpoch = 0;
  let sourceProgress = null,
    progressEtag = "",
    progressRevision = "",
    previewQuality = "proxy";
  const refreshing = new Set();
  const sourceCount = () => session?.selection?.summary?.total || assets.length;
  let auth,
    session = null,
    assets = [],
    analysis = null,
    results = [],
    allWorks = [],
    caps = {},
    view = "source",
    index = 0,
    showTimeline = false,
    platform = "TikTok",
    copies = {},
    busy = false,
    uploading = false,
    mediaKey = "",
    timer,
    messageKey = "",
    modalKind = "",
    sourceSelection = [],
    library = [],
    pollFailures = 0,
    requestReceipt = null;
  let toastTimer,
    copySaveTimer,
    extraResults = [];
  const isActive = (s) =>
    [
      "queued",
      "waiting_sources",
      "analyzing",
      "preparing",
      "rendering",
    ].includes(s);
  const config = () => session?.config || {};
  const finished = () => results.filter((x) => x.status === "completed");
  async function api(path, body, method, options = {}) {
    const response = await fetch(path.startsWith("/api/") ? path : API + path, {
      ...options,
      method: method || (body === undefined ? "GET" : "POST"),
      credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response
      .json()
      .catch(() => ({ error: "服务未返回有效响应" }));
    if (!response.ok) {
      const error = new Error(data.error || data.message || "请求未完成");
      error.code = data.code;
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function toast(text, error = false) {
    clearTimeout(toastTimer);
    $("toast").textContent = text;
    $("toast").classList.toggle("error", error);
    $("toast").hidden = false;
    toastTimer = setTimeout(() => ($("toast").hidden = true), 6500);
  }
  async function action(fn, key = document.activeElement) {
    key ||= fn;
    if (actionLocks.has(key)) return actionLocks.get(key);
    const button =
      key instanceof HTMLElement && key.tagName === "BUTTON" ? key : null;
    if (button) button.disabled = true;
    const work = Promise.resolve()
      .then(fn)
      .catch((e) => {
        if (e.name !== "AbortError") toast(e.message, true);
      })
      .finally(() => {
        actionLocks.delete(key);
        if (button?.isConnected) button.disabled = false;
        busy = actionLocks.has("send");
        updateComposer();
        if (key === "confirm-selection" && modalKind === "assets")
          drawSelection();
      });
    actionLocks.set(key, work);
    busy = actionLocks.has("send");
    updateComposer();
    return work;
  }
  function openModal(title, subtitle, body, footer = "", kind = "") {
    $("modalTitle").textContent = title;
    $("modalSubtitle").textContent = subtitle;
    $("modalBody").innerHTML = body;
    $("modalFooter").innerHTML = footer;
    clearTimeout(worksTimer);
    ++worksEpoch;
    modalKind = kind;
    $("modal").classList.toggle("assets", kind === "assets");
    $("modal").classList.toggle(
      "settings-modal",
      kind === "narration" || kind === "translation",
    );
    iconify($("modal"));
    if (!$("modal").open) $("modal").showModal();
  }
  function closeModal() {
    window.AIEditorStory.close();
    if (uploading) {
      toast("文件正在上传，请稍候");
      return;
    }
    ++libraryEpoch;
    catalogController?.abort();
    assetController?.abort();
    $("stylePreviewVideo")?.pause();
    settingsObserver?.disconnect();
    $("modal").close();
    $("captionPreview").hidden = true;
    modalKind = "";
  }
  function adoptSession(item) {
    const current = view === "source" ? assets[index]?.id : null;
    session = item;
    assets = item.assets;
    sourceProgress = item.selection || null;
    if (current) {
      const found = assets.findIndex((a) => a.id === current);
      if (found >= 0) index = found;
    }
  }
  async function ensureSession() {
    if (session) return session;
    if (!sessionPromise)
      sessionPromise = api("/studio/sessions", { source_ids: [] })
        .then((d) => {
          adoptSession(d.item);
          location.hash = session.id;
          return session;
        })
        .finally(() => {
          sessionPromise = null;
        });
    return sessionPromise;
  }
  function save(change) {
    const sid = session?.id,
      epoch = navigationEpoch;
    const work = saveChain
      .catch(() => {})
      .then(async () => {
        if (epoch !== navigationEpoch || (sid && session?.id !== sid))
          throw new Error("创作已切换，请在当前创作中重新保存");
        await ensureSession();
        let item;
        try {
          item = (
            await api(
              "/studio/sessions/" + session.id,
              { ...change, version: session.version },
              "PATCH",
            )
          ).item;
        } catch (e) {
          if (e.code === "version_conflict") {
            const latest = (await api("/studio/sessions/" + session.id)).item;
            if (epoch === navigationEpoch) adoptSession(latest);
            throw new Error(
              "另一窗口已更新创作，当前表单已保留。请核对配置后再保存。",
            );
          }
          throw e;
        }
        if (epoch === navigationEpoch && session?.id === item.id) {
          adoptSession(item);
          render();
        }
      });
    saveChain = work;
    return work;
  }
  async function loadSession(id) {
    const epoch = ++navigationEpoch;
    const item = (await api("/studio/sessions/" + encodeURIComponent(id))).item;
    if (epoch !== navigationEpoch) return;
    adoptSession(item);
    progressEtag = "";
    progressRevision = "";
    analysis = null;
    results = [];
    extraResults = [];
    index = 0;
    view = "source";
    showTimeline = !!item.analysis_id;
    mediaKey = "";
    messageKey = "";
    requestReceipt = null;
    $("copyPanel").hidden = true;
    location.hash = id;
    await refresh(true, item);
  }
  function queueRefresh() {
    clearTimeout(timer);
    if (!session) return;
    const active =
      sourceProgress?.active ||
      assets.some((a) => ["queued", "running"].includes(a.preview_status)) ||
      session.runs.some((r) => isActive(r.status)) ||
      ["queued", "running"].includes(analysis?.status);
    if (active || pollFailures)
      timer = setTimeout(
        () => refresh(false),
        document.hidden
          ? 10000
          : pollFailures
            ? [4000, 8000, 15000][Math.min(2, pollFailures - 1)]
            : 2000,
      );
  }
  async function refresh(full = true, loadedSession = null) {
    clearTimeout(timer);
    if (!session) return;
    const sid = session.id,
      epoch = navigationEpoch;
    if (refreshing.has(sid)) return;
    refreshing.add(sid);
    try {
      let item;
      // Source ETags do not cover child render task receipts. Keep following an
      // active production even when all imported sources are unchanged.
      if (session.runs.some((r) => r.kind === "create" && isActive(r.status))) full = true;
      if (!full && session.selection_id) {
        const response = await fetch(
          API + "/studio/sessions/" + sid + "/source-progress",
          {
            credentials: "same-origin",
            headers: progressEtag ? { "If-None-Match": progressEtag } : {},
          },
        );
        if (session?.id !== sid || epoch !== navigationEpoch) return;
        if (response.status === 304) {
          pollFailures = 0;
          lastSyncedAt = Date.now();
          renderProduction();
          return;
        }
        if (!response.ok) throw new Error("暂时无法读取素材进度");
        const progress = (await response.json()).selection;
        if (
          !progress ||
          progress.selection_revision < (session.selection_revision || 0)
        )
          return;
        progressEtag = response.headers.get("ETag") || progress.etag;
        const current = assets[index]?.id;
        sourceProgress = progress;
        session.selection = progress;
        assets = progress.items.filter((i) => i.asset).map((i) => i.asset);
        session.assets = assets;
        if (current) {
          const found = assets.findIndex((a) => a.id === current);
          if (found >= 0) index = found;
        }
        if (progress.session_revision !== progressRevision) full = true;
        progressRevision = progress.session_revision;
      } else full = true;
      const oldCompleted = new Set(finished().map((r) => r.id));
      if (full) {
        item = loadedSession || (await api("/studio/sessions/" + sid)).item;
        if (session?.id !== sid || epoch !== navigationEpoch) return;
        adoptSession(item);
        if (sourceProgress) {
          progressEtag = sourceProgress.etag;
          progressRevision = sourceProgress.session_revision;
        }
      }
      results = [
        ...session.runs.flatMap((r) => [...(r.revisions || []), ...r.tasks]),
        ...extraResults.filter(
          (x) =>
            !session.runs.some((r) =>
              [...(r.revisions || []), ...r.tasks].some((t) => t.id === x.id),
            ),
        ),
      ];
      // Source cards and playback are usable before optional analysis details arrive.
      lastSyncedAt = Date.now();
      render();
      if (
        session.analysis_id &&
        (analysis?.analysis_id !== session.analysis_id ||
          !["succeeded", "failed"].includes(analysis?.status))
      ) {
        const data = await api("/analyses/" + session.analysis_id);
        if (session?.id !== sid || epoch !== navigationEpoch) return;
        analysis = data;
        showTimeline = true;
      }
      const newlyCompleted = finished().find((r) => !oldCompleted.has(r.id));
      pollFailures = 0;
      render();
      if (newlyCompleted && !loadedSession)
        toast("成片已制作完成！点击「查看成片」预览，或到右上角「我的作品」下载。");
    } catch (e) {
      if (session?.id === sid && epoch === navigationEpoch) {
        pollFailures++;
        if (pollFailures === 1)
          toast("进度暂不可读取，将自动重连。" + e.message, true);
        renderProduction();
      }
    } finally {
      refreshing.delete(sid);
      if (session?.id === sid && epoch === navigationEpoch) queueRefresh();
    }
  }
  function clipDuration(c) {
    if(c.method==="hook_first") {
      if (!assets.length || assets.length<sourceCount()) return "素材就绪后显示预计总时长";
      const grouping=config().merge===false?assets.map(a=>[a]):[assets];
      const estimates=grouping.map(group=>{
        const windows=(analysis?.sources||[]).filter(s=>group.some(a=>a.id===s.source_id)).flatMap(s=>s.candidates||[]);
        const body=group.reduce((n,a)=>n+(a.duration_ms||a.metadata?.duration_ms||0),0);
        if(!windows.length)return time(body)+" 原片＋开头";
        const added=Math.min(Math.max(...group.map(a=>a.duration_ms||a.metadata?.duration_ms||0)),1800+Math.max(...windows.map(e=>e.end_ms-e.start_ms)));
        return "约 "+time(body+added);
      });
      return estimates.join(" / ");
    }
    return c.seconds?c.seconds+" 秒":c.strategy_version===2?"约 60～90 秒":"自动";
  }
  function configSummary(c) {
    if (c.youtube_regular) {
      const y = c.youtube_regular;
      return `<div class="configuration-summary"><strong>YouTube 普通视频</strong><div>正片 ${y.body_source_ids.length} 条 · ${y.hook_source_ids.length ? "片头从指定的 " + y.hook_source_ids.length + " 条素材选取" : "片头从正片选取"}</div></div>`;
    }
    return `<div class="configuration-summary"><strong>${c.clip ? esc(methods[c.clip.method]?.[0]) : "视频创作"}${c.narration ? " · 影视解说" : ""}${c.translation ? " · 视频翻译" : ""}</strong><div class="summary-grid"><div><span>素材处理</span>${c.merge === false ? "逐条处理" : "合并素材"}</div><div><span>输出数量</span>${(c.merge === false ? sourceCount() : 1) * (c.clip?.script_count || 1) * (c.clip?.variant_count || 1)} 条</div>${c.clip ? `<div><span>目标时长</span>${esc(clipDuration(c.clip))}</div><div><span>脚本 × 裂变</span>${c.clip.script_count} × ${c.clip.variant_count}</div>` : ""}${c.translation ? `<div><span>翻译语种</span>${esc(languageName(c.translation.language))}</div><div><span>字幕</span>${c.translation.mode === "bilingual" ? "双语" : "仅译文"}</div>` : ""}</div></div>`;
  }
  function languageName(code) {
    return caps.languages?.find((x) => x.code === code)?.name || code;
  }
  function renderMessages() {
    const entries = session?.messages || [],
      runs = session?.runs || [];
    const key = JSON.stringify([
      entries,
      runs.map((r) => [
        r.id,
        r.status,
        r.error,
        r.tasks.map((t) => [t.id, t.status]),
      ]),
    ]);
    if (key === messageKey) return;
    messageKey = key;
    const box = $("messages"),
      nearEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    let html = entries.length
      ? ""
      : `<div class="welcome"><div class="welcome-label">YOUR NEXT STORY</div><h2>今天，想创作什么？</h2><p>${assets.length ? "素材已就绪。可以解析剧情、选择剪法，或直接告诉我你的想法。" : "从一段素材开始。\n我会帮你发现故事亮点，把创作想法变成视频。"}</p><button class="prompt-card" data-action="assets">${assets.length ? "管理已选素材" : "选择视频素材"}<span>↗</span></button><button class="prompt-card" data-prompt="请概括视频剧情并指出重要转折。">视频内容总结<span>→</span></button><button class="prompt-card" data-prompt="提供一份适合 TikTok 的短剧剪辑方案。">TikTok 剪辑建议<span>→</span></button><button class="prompt-card" data-prompt="提供一份适合 YouTube Shorts 的剪辑方案。">YouTube 剪辑建议<span>→</span></button></div>`;
    html += entries
      .map(
        (m) =>
          `<article class="message ${m.role === "user" ? "user" : ""}"><div class="message-author">${m.role === "user" ? "你" : `<span class="assistant-avatar">AI</span>创作助手`}</div>${m.kind === "config" ? configSummary(m.data.config) : `<div class="bubble">${esc(m.data.text)}</div>`}${m.data.config && m.kind === "suggestion" ? configSummary(m.data.config) + `<button class="primary" data-suggestion="${esc(m.id)}">编辑并使用此方案</button>` : ""}${m.kind === "script" ? `<button class="quiet" data-script="${esc(m.id)}">使用此解说稿</button>` : ""}${m.data.evidence?.length ? `<div class="citations">${m.data.evidence.map((e, i) => `<button data-cite="${esc(m.id)}" data-cite-index="${i}">${time(e.start_ms)} – ${time(e.end_ms)} ↗</button>`).join("")}</div>` : ""}</article>`,
      )
      .join("");
    html += runs
      .filter((r) => r.kind !== "plan" && (r.kind === "create" || r.status !== "completed"))
      .map((r) => {
        const p = Production.describe(r);
        return `<div class="run-card ${p.tone}"><header><strong>${esc(p.title)}</strong><span>${p.total ? `成片 ${p.completed}/${p.total}` : ""}</span></header><p>${esc(p.detail)}</p>${p.completed ? `<button class="primary" data-run-results="${r.id}">查看成片 ${p.completed} 条</button>` : ""}${r.status === "failed" ? `<button data-retry="${r.id}">重试失败项</button>` : ""}</div>`;
      })
      .join("");
    box.innerHTML = html;
    if (nearEnd || !entries.length) box.scrollTop = box.scrollHeight;
  }
  function updateComposer() {
    const c = config();
    const story = c.clip?.strategy_version===2 && sourceCount()>0;
    $("storyBar").hidden=!story;
    if(story){
      $("storySummary").textContent="爆点引流 · "+clipDuration(c.clip)+" · "+
        (c.clip.story_order==="episode" && session?.story_order_hint?.verified?"按集号顺叙":"按选入顺序");
      $("storyOrderLabel").textContent=window.AIEditorStory.orderText(session,c.clip.story_order);
    }
    $("configChips").innerHTML = [
      "clip",
      "narration",
      "translation",
      "youtube_regular",
    ]
      .filter((k) => c[k])
      .map(
        (k) =>
          `<span class="chip"><button type="button" data-action="${k}">${{ clip: "智能剪辑", narration: "影视解说", translation: "视频翻译", youtube_regular: "YouTube 普通视频" }[k]}</button><button type="button" data-remove-config="${k}" aria-label="移除${k}">×</button></span>`,
      )
      .join("");
    const configured = !!(
        c.clip ||
        c.narration ||
        c.translation ||
        c.youtube_regular
      ),
      count =
        (c.merge === false ? sourceCount() : 1) *
        (c.clip?.script_count || 1) *
        (c.clip?.variant_count || 1);
    $("submitHint").textContent = busy
      ? "正在处理…"
      : !sourceCount()
        ? "先选择素材，开始创作"
        : configured
          ? `预计 ${count} 条作品 · 发送开始制作`
          : "可提问，或添加创作配置";
    $("send").disabled = busy || !sourceCount();
  }
  function currentMedia() {
    return (view === "source" ? assets : finished())[index];
  }
  function renderPlayer() {
    const list = view === "source" ? assets : finished();
    if (index >= list.length) index = 0;
    const media = list[index],
      identity = view + ":" + (media?.id || "") + ":" + previewQuality;
    if (
      view === "source" &&
      media &&
      $("player").dataset.prepared !== media.id
    ) {
      $("player").dataset.prepared = media.id;
      api("/assets/" + media.id + "/preview", {})
        .then(() => refresh(false))
        .catch(() => {});
    }
    const keep = $("player").dataset.identity === identity;
    const url = keep
      ? mediaKey
      : view === "source"
        ? previewQuality === "proxy"
          ? media?.proxy_url || media?.preview_url
          : media?.preview_url
        : media?.output?.preview_url && media.output.preview_url +
          (previewQuality === "original" ? "?quality=original" : "");
    $("player").dataset.identity = identity;
    if (media?.poster_url) $("player").poster = media.poster_url;
    else $("player").removeAttribute("poster");
    $("previewQuality").hidden = !media || (view !== "source" && !media.output?.preview_available);
    $("previewQuality").value =
      media?.proxy_url && url === media.preview_url
        ? "original"
        : previewQuality;
    $("previewHint").textContent =
      view === "source" && media
        ? media.preview_error ||
          (media.proxy_url
            ? mediaKey === media.preview_url
              ? "流畅预览已就绪，可切换"
              : "流畅预览不影响原片导出"
            : "正在后台准备流畅预览，可先播放原片")
        : media?.output?.preview_available ? "流畅预览 · 导出保留原画质" : "";
    if (url !== mediaKey) {
      mediaKey = url || "";
      const player = $("player");
      player.pause();
      $("playerLoading").hidden = !url;
      $("playerLoading").textContent = "正在载入预览…";
      if (url) {player.dataset.loadStarted=performance.now();player.src = url;}
      else {
        player.removeAttribute("src");
        player.load();
      }
    }
    $("player").hidden = !url;
    $("stageEmpty").hidden = !!url;
    $("mediaTitle").textContent = media?.name || "";
    $("sourceView").classList.toggle("active", view === "source");
    $("resultView").classList.toggle("active", view === "result");
    $("resultCount").textContent = finished().length;
    $("exportButton").hidden = view !== "result" || !media;
    $("copyButton").hidden = view !== "result" || !media || !!media.youtube_regular;
    const ordinary = view === "result" && media?.youtube_regular;
    $("regularActions").hidden = !ordinary;
    if (ordinary) {
      const m = media.output.metadata;
      const short = m.width <= m.height && m.duration_ms <= 180000;
      $("regularInfo").textContent =
        `版本 ${media.version} · ${m.width} × ${m.height}` +
        (short
          ? " · 这条成片会被 YouTube 归为 Shorts。要发普通视频，请调整时长或画幅。"
          : "");
    }
    $("stripLabel").innerHTML =
      view === "source" ? "待处理<br>视频" : "已完成<br>作品";
    const strip =
      view === "source" && sourceProgress
        ? sourceProgress.items.map(
            (x) => x.asset || { id: x.id, name: x.name, pending: true },
          )
        : list;
    const stripKey = JSON.stringify([
      view,
      media?.id,
      strip.map((m) => [m.id, m.poster_url, m.pending]),
    ]);
    if ($("filmstrip").dataset.key !== stripKey) {
      $("filmstrip").dataset.key = stripKey;
      $("filmstrip").innerHTML = strip
        .map((m, i) => {
          const actual = list.findIndex((a) => a.id === m.id);
          return `<button class="thumbnail ${media?.id === m.id ? "active" : ""}" data-media="${actual}" ${actual < 0 ? "disabled" : ""} aria-label="素材 ${i + 1}：${esc(m.name)}">${m.poster_url ? `<img src="${esc(m.poster_url)}" alt="" loading="lazy">` : '<span class="poster-placeholder">▶</span>'}<span class="number">${i + 1}</span><span class="duration">${m.pending ? "准备中" : time(m.duration_ms || m.output?.metadata?.duration_ms)}</span></button>`;
        })
        .join("");
    }
    $("stageSuggestions").hidden = !!assets.length;
  }
  function renderProduction() {
    const runs = (session?.runs || []).filter((r) => r.kind === "create" && r.status !== "superseded");
    const run = [...runs].reverse().find((r) => isActive(r.status)) || runs.at(-1);
    const panel = $("productionStatus");
    panel.hidden = !run;
    if (!run) return;
    const p = Production.describe(run);
    const steps = ["素材准备", "生成方案", "排队制作", "制作成片", "成片完成"];
    const elapsed = p.waitSeconds >= 60 ? `${Math.floor(p.waitSeconds / 60)} 分 ${Math.floor(p.waitSeconds % 60)} 秒` : `${Math.floor(p.waitSeconds)} 秒`;
    const created = new Date(run.created_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const counts = p.total ? `成片已完成 ${p.completed} / ${p.total} 条` : "已提交制作任务";
    const details = [p.running ? `制作中 ${p.running} 条` : "", p.queued ? `排队 ${p.queued} 条` : "", p.failed ? `失败 ${p.failed} 条` : ""].filter(Boolean).join(" · ");
    panel.className = `production-status ${p.tone}`;
    panel.innerHTML = `<header><div><span class="production-eyebrow">本次制作</span><h2>${esc(p.title)}</h2></div><strong>${counts}</strong></header>
      <ol class="production-steps" aria-label="制作阶段">${steps.map((label, i) => `<li class="${i < p.phase || p.tone === "complete" ? "done" : i === p.phase ? "current" : ""}" ${i === p.phase ? 'aria-current="step"' : ""}><span>${i < p.phase || p.tone === "complete" ? "✓" : i + 1}</span>${label}</li>`).join("")}</ol>
      <p>${esc(p.detail)}</p>${details ? `<p class="production-counts">${details}</p>` : ""}
      <footer><small>${created} 提交${p.queued && !p.running ? ` · 已排队 ${elapsed}` : ""} · ${pollFailures ? "连接中断，正在重连；保留上次状态" : `状态更新于 ${new Date(lastSyncedAt || Date.now()).toLocaleTimeString()}`}</small><div>
      ${p.completed ? `<button class="primary" data-run-results="${run.id}">查看成片 ${p.completed} 条</button><button data-run-download="${run.id}">下载成片</button>` : ""}
      ${run.status === "failed" ? `<button data-retry="${run.id}">重试失败项</button>` : ""}
      ${run.status === "needs_attention" ? '<button data-action="assets">处理素材</button>' : ""}
      <button data-action="works">我的作品</button></div></footer>`;
  }

  function renderTimeline() {
    $("timelinePanel").hidden =
      !showTimeline || view !== "source" || !assets.length;
    if ($("timelinePanel").hidden) return;
    const source = analysis?.sources?.find(
        (s) => s.source_id === assets[index]?.id,
      ),
      duration = source?.duration_ms || assets[index]?.duration_ms || 1;
    const rows =
      source?.candidates?.filter((c) => c.engine === "multimodal") || [];
    const tracks = [
      ["黄金开场", "#b06498", ["hook"]],
      ["亮点解析", "#ce7e51", ["conflict", "turn"]],
      ["剧情解析", "#a58c43", ["context", "resolution"]],
      ["结尾悬念", "#48969b", ["cliffhanger"]],
    ];
    let html = `<div class="time-scale">${[0, 0.25, 0.5, 0.75, 1].map((n) => `<span>${time(duration * n)}</span>`).join("")}</div>`;
    html += tracks
      .map(
        ([name, color, tags]) =>
          `<div class="track" style="--track:${color}"><span class="track-label"><i class="track-dot"></i>${name}</span><div class="track-lane">${
            rows
              .filter((c) => tags.includes(c.tag))
              .map(
                (c) =>
                  `<button class="track-mark" data-seek="${c.start_ms}" style="left:${(c.start_ms / duration) * 100}%;width:${((c.end_ms - c.start_ms) / duration) * 100}%" title="${esc(c.reason)} · ${time(c.start_ms)}–${time(c.end_ms)}">${esc(c.category + " · " + c.reason)}</button>`,
              )
              .join("") ||
            `<span class="no-data">${analysis?.status === "succeeded" ? "未识别到对应片段" : "解析完成后显示"}</span>`
          }</div></div>`,
      )
      .join("");
    const samples = source?.emotion_samples || [];
    const points = samples
      .map((p) => `${(p.time_ms / duration) * 1000},${48 - p.intensity * 43}`)
      .join(" ");
    html += `<div class="track emotion" style="--track:#548bbf"><span class="track-label"><i class="track-dot"></i>情绪张力</span><div class="track-lane">${samples.length > 1 ? `<svg viewBox="0 0 1000 50" preserveAspectRatio="none" aria-label="画面情绪张力采样曲线"><polygon points="${(samples[0].time_ms / duration) * 1000},50 ${points} ${(samples.at(-1).time_ms / duration) * 1000},50" fill="#dfeafa"/><polyline points="${points}" fill="none" stroke="#6f99c9" stroke-width="1.5"/></svg>` : '<span class="no-data">暂无足够的情绪采样</span>'}</div></div><div class="playhead" id="playhead"></div>`;
    $("timeline").innerHTML = html;
    updatePlayhead();
  }
  function updatePlayhead() {
    const head = $("playhead"),
      video = $("player");
    if (head && Number.isFinite(video.duration)) {
      const width = $("timeline").clientWidth - 134;
      head.style.left =
        120 + Math.min(1, video.currentTime / video.duration) * width + "px";
    }
  }
  function render() {
    $("sessionTitle").textContent = session?.title || "新建创作";
    $("sessionState").textContent = sourceCount()
      ? sourceProgress
        ? preparationSummary(sourceProgress)
        : `已选 ${assets.length} 个素材 · ${analysis?.status === "succeeded" ? "解析完成" : "随时添加创作配置"}`
      : "和 AI 一起完成你的下一条视频";
    renderPreparation();
    renderMessages();
    renderProduction();
    updateComposer();
    renderPlayer();
    renderTimeline();
  }
  async function analyze() {
    if (!sourceCount()) return openAssets();
    await ensureSession();
    await api("/studio/sessions/" + session.id + "/analyze", {
      version: session.version,
      config: config(),
      operation_id: op(),
    });
    showTimeline = true;
    view = "source";
    await refresh();
  }
  const planOperations = new Map();
  async function produce(c, planId) {
    await saveChain;
    const signature=JSON.stringify([session.id,session.selection_id || assets.map(a=>a.id),c,planId]);
    if (!requestReceipt || requestReceipt.signature!==signature) requestReceipt={signature,operation:op()};
    await api("/studio/sessions/"+session.id+"/submit",{
      config:c,version:session.version,operation_id:requestReceipt.operation,...(planId?{edit_plan_id:planId}:{})
    });
    requestReceipt=null;
    toast("已提交，云端将持续制作");
    await refresh();
  }
  async function openStory() {
    await saveChain;
    return window.AIEditorStory.open({
      api,esc,modal:openModal,get:()=>session,act:action,
      settings:()=>openConfig("clip"),
      sync:async(item)=>{if(item && item.id===session?.id && item.version>=session.version)adoptSession(item);await refresh(true);},
      change:async(key)=>{
        const c=config();await save({config:{...c,clip:{...c.clip,[key]:(c.clip[key]||0)+1}}});
      },
      request:async(s)=>{
        const sig=JSON.stringify([s.id,s.selection_id||s.source_ids,s.config]);
        if(!planOperations.has(sig))planOperations.set(sig,op());
        const answer=await api("/studio/sessions/"+s.id+"/edit-plan",{config:s.config,version:s.version,operation_id:planOperations.get(sig)});
        await refresh();
        return answer;
      },
      submit:async(pid)=>{
        if(session.edit_plan?.id!==pid)throw new Error("剪辑方案已更新，请重新打开核对");
        await produce(config(),pid);closeModal();
      },
    });
  }
  async function send() {
    await saveChain.catch(() => {});
    if (!sourceCount()) return openAssets();
    const text = $("prompt").value.trim(),
      c = config();
    await ensureSession();
    if (
      !text &&
      (c.clip || c.narration || c.translation || c.youtube_regular)
    ) {
      await produce(c,session.edit_plan?.id);
      $("prompt").value = "";
    } else if (text) {
      if (analysis?.status !== "succeeded") {
        await analyze();
        toast("已开始解析素材，完成后即可提问");
        return;
      }
      const signature = JSON.stringify([session.id, text]);
      if (!requestReceipt || requestReceipt.signature !== signature)
        requestReceipt = { signature, operation: op() };
      await api("/studio/sessions/" + session.id + "/messages", {
        text,
        operation_id: requestReceipt.operation,
      });
      requestReceipt = null;
      $("prompt").value = "";
      await refresh();
    } else openConfig("clip");
  }
  let librarySource = "local",
    catalogPage = 1,
    catalogItems = [],
    catalogDrama = null,
    catalogEpisodes = [];
  let libraryEpoch = 0,
    catalogController,
    assetController,
    libraryOffset = 0,
    libraryTotal = 0,
    catalogImports = [];
  const libraryCache = new Map();
  const browseRequests = new Map();
  let browseStorageLoaded = "";
  const canCacheBrowse = () => auth?.user?.is_admin === true || auth?.user?.role === "admin" || auth?.user?.permissions?.video_editing === true;
  const browseStorageKey = () => "editor-browse-v1:" + JSON.stringify([
    auth?.user?.tenant_key, auth?.user?.user_id || auth?.name,
  ]);
  function cachedBrowse(key) {
    if (!canCacheBrowse()) return null;
    const storageKey = browseStorageKey();
    if (browseStorageLoaded !== storageKey) {
      browseStorageLoaded = storageKey;
      for (const name of libraryCache.keys())
        if (/^(catalog|episodes):/.test(name)) libraryCache.delete(name);
      try {
        for (const [name, entry] of JSON.parse(sessionStorage.getItem(storageKey) || "[]"))
          if (Date.now() - entry.at < 86400000) libraryCache.set(name, entry);
      } catch {}
    }
    const cached = libraryCache.get(key);
    return cached && Date.now() - cached.at < 86400000 ? cached : null;
  }
  function storeBrowse(key, data) {
    if (!canCacheBrowse()) return;
    cachedBrowse(key);
    libraryCache.set(key, { data, at: Math.min(Date.now(), (data.cache?.updated_at || Date.now()/1000)*1000) });
    try {
      const entries = [...libraryCache].filter(([name]) => /^(catalog|episodes):/.test(name));
      entries.sort((a,b) => b[1].at-a[1].at);
      sessionStorage.setItem(browseStorageKey(), JSON.stringify(entries.slice(0,40)));
    } catch {}
  }
  function readBrowse(key, url, signal, force = false) {
    let prior = browseRequests.get(key);
    if (prior?.signal?.aborted) {
      browseRequests.delete(key);
      prior = null;
    }
    if (prior) {
      if (force && !prior.force)
        return prior.promise.catch(() => {}).then(() => readBrowse(key, url, signal, true));
      return prior.promise;
    }
    const promise = api(url + (force ? "&refresh=1" : ""), undefined, undefined, { signal })
      .then((data) => { storeBrowse(key, data); return data; })
      .finally(() => {
        if (browseRequests.get(key)?.promise === promise) browseRequests.delete(key);
      });
    browseRequests.set(key, { promise, force, signal });
    return promise;
  }
  function browseStatus(data, updating = false) {
    const node = $("catalogReadStatus");
    if (!node) return;
    const at = data?.cache?.updated_at;
    node.textContent = data
      ? `${at ? "更新于 " + new Date(at*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}) : "已显示上次结果"}${updating ? " · 正在更新，可继续选择" : ""}${data.cache?.error ? " · 更新暂不可用，保留上次结果" : ""}`
      : "正在读取，可随时切换来源或返回";
  }
  function prefetchBrowse(key, url) {
    const cached = cachedBrowse(key);
    if ((cached && Date.now()-cached.at < 300000) || browseRequests.size >= 2) return;
    readBrowse(key, url).catch(() => {});
  }
  function warmCatalogs() {
    for (const product of ["dramawave", "moboreels"])
      prefetchBrowse("catalog:" + JSON.stringify([product,1,""]), "/catalog/dramas?" + new URLSearchParams({product,page:1,q:""}));
  }
  const draftKey = () =>
    "editor-selection:" +
    JSON.stringify([
      auth?.user?.tenant_key,
      auth?.user?.user_id || auth?.name,
      session?.id || "new",
    ]);
  const pendingKey = () =>
    "editor-selection-operation:" +
    JSON.stringify([auth?.user?.tenant_key, auth?.user?.user_id || auth?.name]);
  function persistDraft() {
    try {
      sessionStorage.setItem(
        draftKey(),
        JSON.stringify({
          items: sourceSelection,
          version: session?.version,
          merge: $("mergeSources")?.checked !== false,
        }),
      );
    } catch {}
  }
  function preparationSummary(progress) {
    const n = progress.summary;
    return `已选 ${n.total} 个 · 导入完成 ${n.imported}/${n.total} · 解析完成 ${n.analyzed}/${n.total}${n.failed ? ` · ${n.failed} 个需处理` : ""}`;
  }
  const importLabels = {
    queued: "等待导入",
    resolving: "获取地址",
    downloading: "下载中",
    validating: "校验中",
    ready: "已就绪",
    failed: "导入失败",
  };
  const analysisLabels = {
    waiting_import: "等待素材",
    queued: "等待解析",
    running: "解析中",
    succeeded: "解析完成",
    failed: "解析失败",
  };
  function renderPreparation() {
    const panel = $("sourcePreparation");
    panel.hidden = !sourceProgress;
    if (!sourceProgress) return;
    $("sourcePreparationSummary").textContent =
      preparationSummary(sourceProgress);
    const signature = JSON.stringify(
      sourceProgress.items.map((i) => [
        i.id,
        i.import_stage,
        i.analysis_status,
        i.bytes,
        i.total,
        i.speed,
        i.error,
        i.asset?.preview_status,
        i.asset?.poster_url,
      ]),
    );
    if (panel.dataset.key === signature) return;
    panel.dataset.key = signature;
    $("sourcePreparationItems").innerHTML = sourceProgress.items
      .map((item, i) => {
        const downloading = item.import_status === "downloading";
        const bytes = item.bytes
          ? ` · ${(item.bytes / 1048576).toFixed(1)}${item.total ? " / " + (item.total / 1048576).toFixed(1) : ""} MB`
          : "";
        const speed =
          downloading && item.speed > 0
            ? ` · ${(item.speed / 1048576).toFixed(2)} MB/s`
            : "";
        const preview = item.asset
          ? item.asset.preview_status === "ready"
            ? "可播放"
            : item.asset.preview_status === "failed"
              ? "准备失败，原片可用"
              : item.asset.poster_url
                ? "预览准备中"
                : "封面准备中"
          : "等待原片";
        return `<div class="source-progress-item"><span class="source-position">${i + 1}</span><div><strong>${esc(item.name)}</strong><small>导入：${importLabels[item.import_stage] || importLabels[item.import_status]}${bytes}${speed} · 解析：${analysisLabels[item.analysis_status] || "等待解析"}</small><small>预览：${preview}${item.asset?.preview_error ? " · " + esc(item.asset.preview_error) : ""}</small>${item.error ? `<p class="source-error">${esc(item.error)}</p>` : ""}${item.warnings?.length ? `<small>${esc(item.warnings.join("；"))}</small>` : ""}${downloading ? `<progress ${item.total ? `max="${item.total}" value="${item.bytes}"` : ""}></progress>` : ""}</div>${item.asset ? `<button data-ready-source="${item.asset_id}">预览</button>` : ""}${item.asset?.preview_status === "failed" ? `<button data-preview-retry="${item.asset_id}">重试预览</button>` : ""}${item.error ? `<button data-source-retry="${item.id}">重试</button><button data-source-remove="${item.id}">移除</button>` : ""}</div>`;
      })
      .join("");
  }
  function libraryMarkup() {
    return `<div class="library-tabs" role="tablist" aria-label="媒体来源"><button data-library-source="local" class="active">我的素材</button><button data-library-source="dramawave">DramaWave 短剧</button><button data-library-source="moboreels">MoboReels 短剧</button></div><section id="catalogPanel" hidden><div class="modal-tools"><input id="catalogQuery" type="search" placeholder="输入剧名或剧目 ID"><button id="searchCatalog">搜索剧目</button></div><div class="catalog-summary"><span id="catalogReadStatus" class="muted" role="status"></span><button id="refreshCatalog">更新列表</button></div><div id="catalogContent"></div></section>`;
  }
  function cachedImport(episode) {
    return catalogImports.find(
      (j) =>
        j.product === librarySource &&
        j.source_key === catalogDrama.key &&
        j.episode === episode,
    );
  }
  function episodeIntent(episode) {
    const cached = cachedImport(episode);
    return {
      kind: "catalog",
      product: librarySource,
      key: catalogDrama.key,
      episode,
      name: `${catalogDrama.name} · 第 ${episode} 集`,
      ...(cached?.asset_id ? { asset_id: cached.asset_id } : {}),
    };
  }
  function connectLibrary() {
    $("modalBody")
      .querySelectorAll("[data-library-source]")
      .forEach(
        (button) =>
          (button.onclick = () => {
            librarySource = button.dataset.librarySource;
            catalogDrama = null;
            catalogPage = 1;
            catalogController?.abort();
            ++libraryEpoch;
            $("modalBody")
              .querySelectorAll("[data-library-source]")
              .forEach((b) => b.classList.toggle("active", b === button));
            $("localLibrary").hidden = librarySource !== "local";
            $("catalogPanel").hidden = librarySource === "local";
            if (librarySource === "local")
              readAssets();
            else readCatalog();
          }),
      );
    $("searchCatalog").onclick = () => {
      catalogPage = 1;
      readCatalog();
    };
    $("refreshCatalog").onclick = () => catalogDrama ? openDrama(catalogDrama, true) : readCatalog(true);
    $("catalogQuery").onkeydown = (e) => {
      if (e.key === "Enter") $("searchCatalog").click();
    };
  }
  async function readCatalog(force = false) {
    catalogController?.abort();
    catalogController = new AbortController();
    catalogDrama = null;
    ++libraryEpoch;
    const epoch = libraryEpoch,
      source = librarySource,
      page = catalogPage,
      q = $("catalogQuery").value;
    const key = "catalog:" + JSON.stringify([source, page, q]);
    const paint = (data) => {
      catalogItems = data.items;
      $("catalogContent").innerHTML =
        `<div class="catalog-summary"><strong>选择剧目</strong><span>第 ${page} 页 · 按语言区分版本</span></div><div class="catalog-grid">${data.items.map((d, i) => `<button class="drama-card" data-drama-index="${i}"><img loading="lazy" referrerpolicy="no-referrer" src="${esc(d.cover || "")}" alt=""><span class="card-body"><strong>${esc(d.name)}</strong><small>${esc(d.language || "未知语言")} · ID ${esc(d.id)}</small></span></button>`).join("") || '<p class="modal-empty">没有匹配剧目</p>'}</div><div class="catalog-summary"><button id="catalogPrev" ${page === 1 ? "disabled" : ""}>上一页</button><button id="catalogNext" ${!data.has_more ? "disabled" : ""}>下一页</button></div>`;
      $("catalogContent")
        .querySelectorAll("[data-drama-index]")
        .forEach(
          (b) => {
            const drama = data.items[Number(b.dataset.dramaIndex)];
            b.onclick = () => openDrama(drama);
            let intentTimer;
            const prefetch = () => prefetchBrowse("episodes:" + source + ":" + drama.key,
              "/catalog/episodes?" + new URLSearchParams({product:source,key:drama.key}));
            b.onpointerenter = () => { intentTimer = setTimeout(prefetch, 180); };
            b.onpointerleave = () => clearTimeout(intentTimer);
            b.onfocus = prefetch;
          },
        );
      $("catalogPrev").onclick = () => {
        catalogPage--;
        readCatalog();
      };
      $("catalogNext").onclick = () => {
        catalogPage++;
        readCatalog();
      };
    };
    const cached = cachedBrowse(key);
    if (cached) paint(cached.data);
    else
      $("catalogContent").innerHTML =
        '<p class="modal-empty">正在读取剧目…</p>';
    const fresh = cached && Date.now() - cached.at < 300000;
    browseStatus(cached?.data, !!cached && (force || !fresh));
    if (fresh && !force) return;
    try {
      const url = "/catalog/dramas?" + new URLSearchParams({ product: source, page, q });
      let data = await readBrowse(key, url, catalogController.signal, force === true || !!cached);
      if (
        epoch !== libraryEpoch ||
        modalKind !== "assets" ||
        source !== librarySource
      )
        return;
      paint(data);
      browseStatus(data, data.cache?.state === "stale");
      if (data.cache?.state === "stale") {
        data = await readBrowse(key, url, catalogController.signal, true);
        if (epoch !== libraryEpoch || modalKind !== "assets" || source !== librarySource) return;
        paint(data);browseStatus(data);
      }
    } catch (e) {
      if (
        e.name !== "AbortError" &&
        epoch === libraryEpoch &&
        modalKind === "assets"
      ) {
        if ($("catalogReadStatus")) $("catalogReadStatus").textContent = "更新失败，已有结果和选择已保留，可点击更新列表重试";
        toast("剧目读取失败，选择已保留。" + e.message, true);
      }
    }
  }
  async function openDrama(drama, force = false) {
    catalogController?.abort();
    catalogController = new AbortController();
    catalogDrama = drama;
    ++libraryEpoch;
    const epoch = libraryEpoch,
      source = librarySource,
      key = "episodes:" + source + ":" + drama.key;
    const paint = (data) => {
      catalogEpisodes = data.items;
      $("catalogContent").innerHTML =
        `<div class="catalog-summary"><button id="backCatalog">‹ 返回剧目</button><strong>${esc(drama.name)}</strong><span>${esc(drama.language)} · ${data.items.length} 集</span></div><p class="muted">勾选后加入下方完整选择，确认后在后台导入和解析。每次最多 20 个素材。</p><div class="catalog-summary"><button id="selectFirstEpisodes">选择前 3 集</button><button id="clearEpisodes">清空本剧选择</button><span id="episodeCount"></span></div><div class="episode-grid">${data.items.map((e) => `<label><input type="checkbox" name="catalogEpisode" value="${e.number}">第 ${e.number} 集</label>`).join("")}</div>`;
      $("backCatalog").onclick = () => readCatalog();
      $("catalogContent")
        .querySelectorAll('[name="catalogEpisode"]')
        .forEach(
          (input) =>
            (input.onchange = () => {
              try {
                sourceSelection = Sources.toggle(
                  sourceSelection,
                  episodeIntent(Number(input.value)),
                );
                drawSelection();
              } catch (e) {
                input.checked = false;
                toast(e.message, true);
              }
            }),
        );
      $("selectFirstEpisodes").onclick = () => {
        try {
          let next = [...sourceSelection];
          for (const e of data.items.slice(0, 3)) {
            const item = episodeIntent(e.number);
            if (!next.some((x) => Sources.same(x, item)))
              next = Sources.toggle(next, item);
          }
          sourceSelection = next;
          drawSelection();
        } catch (e) {
          toast(e.message, true);
        }
      };
      $("clearEpisodes").onclick = () => {
        sourceSelection = sourceSelection.filter(
          (x) =>
            !(
              x.kind === "catalog" &&
              x.product === source &&
              x.key === drama.key
            ),
        );
        drawSelection();
      };
      drawSelection();
    };
    const cached = cachedBrowse(key);
    if (cached) paint(cached.data);
    else {
      $("catalogContent").innerHTML =
        `<div class="catalog-summary"><button id="backCatalog">‹ 返回剧目</button><strong>${esc(drama.name)}</strong></div><p class="modal-empty">正在读取分集…</p>`;
      $("backCatalog").onclick = () => readCatalog();
    }
    const fresh = cached && Date.now() - cached.at < 300000;
    browseStatus(cached?.data, !!cached && (force || !fresh));
    if (fresh && !force) return;
    try {
      const url = "/catalog/episodes?" + new URLSearchParams({ product: source, key: drama.key });
      let data = await readBrowse(key, url, catalogController.signal, force === true || !!cached);
      if (
        epoch !== libraryEpoch ||
        modalKind !== "assets" ||
        catalogDrama?.key !== drama.key
      )
        return;
      paint(data);
      browseStatus(data, data.cache?.state === "stale");
      if (data.cache?.state === "stale") {
        data = await readBrowse(key, url, catalogController.signal, true);
        if (epoch !== libraryEpoch || modalKind !== "assets" || catalogDrama?.key !== drama.key) return;
        paint(data);browseStatus(data);
      }
    } catch (e) {
      if (
        e.name !== "AbortError" &&
        epoch === libraryEpoch &&
        modalKind === "assets"
      ) {
        if ($("catalogReadStatus")) $("catalogReadStatus").textContent = "更新失败，已有分集和选择已保留，可点击更新列表重试";
        toast("分集读取失败，已有选择保留。" + e.message, true);
      }
    }
  }
  async function confirmSelection(afterSave) {
    if (!sourceSelection.length) throw new Error("请至少选择一个素材");
    await saveChain.catch(() => {});
    let sid = session?.id || null;
    const epoch = navigationEpoch,
      oldDraft = draftKey();
    const payload = {
      items: sourceSelection.map(Sources.intent),
      config: { ...config(), merge: $("mergeSources").checked },
      ...(sid ? { version: session.version } : {}),
    };
    let signature = JSON.stringify([sid, payload]),
      pending;
    try {
      pending = JSON.parse(sessionStorage.getItem(pendingKey()) || "null");
    } catch {}
    if (pending && pending.signature !== signature) {
      const prior = (
        await api(
          "/studio/selection-operations?" +
            new URLSearchParams({ operation_id: pending.body.operation_id }),
        )
      ).receipt;
      const settled = prior
        ? await api("/studio/sessions/" + prior.session_id)
        : await api(
            pending.sid
              ? "/studio/sessions/" + pending.sid + "/selection"
              : "/studio/sessions",
            pending.body,
          );
      if (epoch !== navigationEpoch) return;
      if (pending.sid === sid) {
        adoptSession(settled.item);
        sid = session.id;
        payload.version = session.version;
        signature = JSON.stringify([sid, payload]);
      }
      sessionStorage.removeItem(pendingKey());
      pending = null;
    }
    const recovering = !!pending;
    if (!pending) {
      pending = { signature, sid, body: { ...payload, operation_id: op() } };
      sessionStorage.setItem(pendingKey(), JSON.stringify(pending));
    }
    let saved;
    try {
      const previous = recovering
        ? (
            await api(
              "/studio/selection-operations?" +
                new URLSearchParams({
                  operation_id: pending.body.operation_id,
                }),
            )
          ).receipt
        : null;
      saved = previous
        ? await api("/studio/sessions/" + previous.session_id)
        : await api(
            sid ? "/studio/sessions/" + sid + "/selection" : "/studio/sessions",
            pending.body,
          );
    } catch (e) {
      if (e.code === "version_conflict") {
        const latest = (await api("/studio/sessions/" + sid)).item;
        if (session?.id === sid) {
          adoptSession(latest);
          persistDraft();
        }
        sessionStorage.removeItem(pendingKey());
        throw new Error(
          "另一窗口已更新创作，你的选择草稿已保留。核对后再次确认即可。",
        );
      }
      throw e;
    }
    sessionStorage.removeItem(pendingKey());
    sessionStorage.removeItem(oldDraft);
    if (epoch !== navigationEpoch) return;
    adoptSession(saved.item);
    location.hash = session.id;
    view = "source";
    index = 0;
    analysis = null;
    progressEtag = "";
    progressRevision = "";
    closeModal();
    render();
    queueRefresh();
    toast(`已保存完整 ${sourceCount()} 个素材，后台将逐集准备`);
    if (afterSave) await afterSave();
  }
  async function openAssets(afterSave) {
    ++libraryEpoch;
    librarySource = "local";
    catalogDrama = null;
    libraryOffset = 0;
    sourceSelection = Sources.unique(
      session?.selection?.items.map(Sources.intent) ||
        assets.map((a) => ({ kind: "asset", asset_id: a.id, name: a.name })),
    );
    let draft;
    try {
      draft = JSON.parse(sessionStorage.getItem(draftKey()) || "null");
    } catch {}
    if (draft?.items && draft.version === session?.version)
      sourceSelection = Sources.unique(draft.items);
    openModal(
      "媒资库",
      "保留完整选择，导入和解析在后台逐集完成",
      `${libraryMarkup()}<section id="localLibrary"><div class="modal-tools"><input id="assetQuery" type="search" placeholder="搜索素材或剧名"><button id="searchAssets">查询</button><button id="toggleUpload" class="primary" data-icon="plus">上传视频</button></div><div id="uploadZone" class="upload-zone" hidden><input id="uploadFile" type="file" accept=".mp4,.mov" multiple><div class="form-grid"><label class="field">剧名（选填）<input id="uploadSeries" maxlength="80"></label><label class="field">起始集号（选填）<input id="uploadEpisode" type="number" min="1" max="100000"></label></div><p>每个视频最多 1 GiB</p><progress id="uploadProgress" max="100" value="0" hidden></progress><p id="uploadStatus"></p><button id="uploadStart">开始上传</button></div><p id="assetListStatus" class="muted"></p><div id="assetGrid" class="asset-grid"></div><button id="moreAssets" hidden>加载更多</button></section><p class="library-selection-title">完整选择 · 按下方顺序创作</p><div id="selectionTray" class="selection-tray"></div>`,
      `<label class="checks"><input type="checkbox" id="mergeSources" ${draft?.merge === false || config().merge === false ? "" : "checked"}>合并所选素材</label><span id="assetCount"></span><button id="confirmAssets" class="primary">确认选择并后台准备</button>`,
      "assets",
    );
    $("toggleUpload").onclick = () =>
      ($("uploadZone").hidden = !$("uploadZone").hidden);
    $("searchAssets").onclick = () => {
      libraryOffset = 0;
      readAssets();
    };
    $("assetQuery").onkeydown = (e) => {
      if (e.key === "Enter") $("searchAssets").click();
    };
    $("moreAssets").onclick = () =>
      action(() => readAssets(true), "more-assets");
    $("uploadStart").onclick = () => action(uploadFiles, "upload-files");
    $("confirmAssets").onclick = () =>
      action(() => confirmSelection(afterSave), "confirm-selection");
    $("mergeSources").onchange = persistDraft;
    connectLibrary();
    drawSelection();
    warmCatalogs();
    api("/catalog/imports")
      .then((data) => {
        // Import identities are account-wide; a source/tab change does not invalidate them.
        catalogImports = data.items;
        if (modalKind === "assets") {
          sourceSelection = Sources.unique(sourceSelection.map((item) => {
            if (item.kind !== "catalog") return item;
            const found = catalogImports.find((job) => job.product === item.product &&
              job.source_key === item.key && job.episode === item.episode && job.asset_id);
            return found ? { ...item, asset_id: found.asset_id } : item;
          }));
          drawSelection();
        }
      })
      .catch(() => {});
    readAssets();
  }
  async function readAssets(more = false) {
    assetController?.abort();
    assetController = new AbortController();
    const epoch = libraryEpoch,
      q = $("assetQuery").value,
      offset = more ? library.length : 0,
      key = "assets:" + q + ":" + offset;
    const paint = (data) => {
      library = more
        ? [
            ...library,
            ...data.items.filter((a) => !library.some((x) => x.id === a.id)),
          ]
        : data.items;
      libraryTotal = data.total;
      drawAssets();
      $("moreAssets").hidden = library.length >= libraryTotal;
      $("assetListStatus").textContent =
        `已显示 ${library.length} / ${libraryTotal} 个素材`;
    };
    const cached = libraryCache.get(key);
    if (cached && !more) paint(cached.data);
    if (!cached)
      $("assetListStatus").textContent =
        "正在读取素材，可继续切换来源或选择分集…";
    try {
      const data = await api(
        "/assets?" + new URLSearchParams({ q, limit: 24, offset }),
        undefined,
        undefined,
        { signal: assetController.signal },
      );
      if (
        epoch !== libraryEpoch ||
        modalKind !== "assets" ||
        $("assetQuery").value !== q
      )
        return;
      libraryCache.set(key, { data, at: Date.now() });
      paint(data);
    } catch (e) {
      if (
        e.name !== "AbortError" &&
        epoch === libraryEpoch &&
        modalKind === "assets"
      )
        $("assetListStatus").textContent =
          "读取失败，已有选择保留：" + e.message;
    }
  }
  function drawAssets() {
    $("assetGrid").innerHTML =
      library
        .map(
          (a) =>
            `<button class="asset-card" data-asset="${a.id}">${a.poster_url ? `<img loading="lazy" src="${esc(a.poster_url)}" alt="">` : '<span class="poster-placeholder">▶</span>'}<span class="card-check">+</span><span class="card-body"><strong>${esc(a.name)}</strong><small>${esc(a.series_code || "本地视频")} · ${time(a.duration_ms)}</small></span></button>`,
        )
        .join("") || '<p class="modal-empty">暂无素材</p>';
    drawSelection();
  }
  function drawSelection() {
    if (modalKind !== "assets") return;
    $("assetGrid")
      .querySelectorAll("[data-asset]")
      .forEach((button) => {
        const position = sourceSelection.findIndex(
          (x) => x.asset_id === button.dataset.asset,
        );
        button.classList.toggle("selected", position >= 0);
        button.querySelector(".card-check").textContent =
          position >= 0 ? position + 1 : "+";
      });
    let checked = 0;
    $("catalogContent")
      .querySelectorAll('[name="catalogEpisode"]')
      .forEach((input) => {
        input.checked = sourceSelection.some((x) =>
          Sources.same(x, episodeIntent(Number(input.value))),
        );
        if (input.checked) checked++;
      });
    if ($("episodeCount"))
      $("episodeCount").textContent = `本剧已选 ${checked} 集`;
    $("selectionTray").innerHTML =
      sourceSelection
        .map(
          (item, i) =>
            `<div class="selection-item">${i + 1}. ${esc(item.name || "已选素材")}<button data-move-source="${i}" data-dir="-1" ${i === 0 ? "disabled" : ""} aria-label="上移">↑</button><button data-move-source="${i}" data-dir="1" ${i === sourceSelection.length - 1 ? "disabled" : ""} aria-label="下移">↓</button><button data-remove-source="${i}" aria-label="移除素材">×</button></div>`,
        )
        .join("") || '<span class="muted">勾选分集或选择已有素材</span>';
    $("assetCount").textContent = `已选 ${sourceSelection.length} 个`;
    $("confirmAssets").textContent =
      `确认 ${sourceSelection.length} 个素材并后台准备`;
    $("confirmAssets").disabled =
      !sourceSelection.length || actionLocks.has("confirm-selection");
    persistDraft();
  }
  async function uploadFiles() {
    if (uploading) return;
    const files = Array.from($("uploadFile").files);
    if (!files.length) throw new Error("请选择文件");
    uploading = true;
    $("uploadStart").disabled = true;
    $("uploadFile").disabled = true;
    $("uploadSeries").disabled = true;
    $("uploadEpisode").disabled = true;
    $("uploadProgress").hidden = false;
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        if (!/\.(mp4|mov)$/i.test(file.name) || file.size > 1024 ** 3)
          throw new Error("请选择不超过 1 GiB 的 MP4 或 MOV");
        $("uploadStatus").textContent =
          `正在上传 ${i + 1}/${files.length}：${file.name}`;
        const item = await window.AIEditorUpload.upload({
          file,
          series: $("uploadSeries").value,
          episode: $("uploadEpisode").value
            ? Number($("uploadEpisode").value) + i
            : 0,
          actor: auth.user || auth.name || "signed-in",
          api,
          onProgress: ({ percent, text }) => {
            $("uploadProgress").value = percent;
            $("uploadStatus").textContent =
              `${i + 1}/${files.length}：${file.name} · ${text}`;
          },
        });
        if (!sourceSelection.some((x) => x.asset_id === item.id))
          sourceSelection.push({
            kind: "asset",
            asset_id: item.id,
            name: item.name,
          });
        persistDraft();
      }
      $("uploadStatus").textContent = "上传完成，已自动选中";
      await readAssets();
    } finally {
      uploading = false;
      for (const id of [
        "uploadStart",
        "uploadFile",
        "uploadSeries",
        "uploadEpisode",
      ])
        if ($(id)) $(id).disabled = false;
    }
  }
  const field = (name, label, control, extra = "") =>
    `<label class="field ${extra}">${label}${control}</label>`;
  const input = (id, value, attrs = "") =>
    `<input id="${id}" value="${esc(value)}" ${attrs}>`;
  const select = (id, options, value) =>
    `<select id="${id}">${options.map(([v, n]) => `<option value="${esc(v)}" ${String(v) === String(value) ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>`;
  let settingsObserver;
  function settingsPreviewMarkup() {
    const source = assets[Math.min(index, assets.length - 1)] || assets[0];
    if (!source)
      return `<aside class="settings-preview"><h3>字幕位置预览</h3><p>所选素材正在准备，可先保存字幕设置。</p></aside>`;
    return `<aside class="settings-preview"><h3>字幕位置预览</h3><p class="muted">拖动高度滑块，字幕即时移动</p>
      <div id="stylePreviewFrame" class="settings-preview-frame"><video id="stylePreviewVideo" muted playsinline preload="metadata" src="${esc(source.preview_url)}"></video><div id="heightGuide" class="height-guide"></div><div id="stylePreviewCaption" class="settings-preview-caption"></div></div>
      <div class="preview-playback"><button type="button" id="toggleStylePlayback">播放预览</button><span id="stylePreviewTime">0:00</span></div>
      <input id="stylePreviewSeek" aria-label="预览画面时间" type="range" min="0" max="${source.duration_ms / 1000}" step="0.1" value="0">
      <output id="stylePositionValue" class="preview-position" aria-live="polite"></output><p class="muted">预览使用所选原片和示例字幕，比例与 720 × 1280 成片一致。</p></aside>`;
  }
  function connectSettingsPreview(kind) {
    const video = $("stylePreviewVideo"),
      frame = $("stylePreviewFrame"),
      caption = $("stylePreviewCaption");
    if (!video || !frame || !caption) return;
    $("player").pause();
    const samples = {
      zh: "门外的脚步声越来越近。",
      en: "The footsteps outside are getting closer.",
      ja: "ドアの外から足音が近づいてくる。",
      ko: "문밖의 발소리가 점점 가까워진다.",
      es: "Los pasos se acercan a la puerta.",
      fr: "Les pas se rapprochent de la porte.",
    };
    const preview = () => {
      const s = readStyle(),
        scale = frame.clientWidth / 720;
      const example = samples[$("language").value] || "Let the story continue.";
      caption.textContent =
        kind === "narration" && $("scriptText").value.trim()
          ? $("scriptText").value.trim().slice(0, 85)
          : example;
      if (kind === "translation" && $("subtitleMode").value === "bilingual")
        caption.textContent = "Someone is coming to the door.\n" + example;
      caption.style.bottom = 100 - s.height + "%";
      caption.style.fontSize = s.size * scale + "px";
      caption.style.color = s.color;
      caption.style.textAlign = s.align;
      caption.style.webkitTextStroke = s.outline * scale + "px #111";
      caption.style.paintOrder = "stroke fill";
      caption.style.textShadow = `0 ${scale}px 0 #111`;
      $("heightGuide").style.bottom = 100 - s.height + "%";
      $("stylePositionValue").textContent =
        `距顶部 ${s.height}% · 距底部 ${100 - s.height}%`;
      $("heightValue").textContent = s.height + "%";
    };
    [
      "styleSize",
      "styleHeight",
      "styleColor",
      "styleAlign",
      "styleOutline",
      "language",
      "subtitleMode",
      "scriptText",
    ].forEach((k) => {
      if ($(k)) $(k).addEventListener("input", preview);
    });
    document.querySelectorAll("[data-caption-height]").forEach(
      (b) =>
        (b.onclick = () => {
          $("styleHeight").value = b.dataset.captionHeight;
          preview();
        }),
    );
    $("toggleStylePlayback").onclick = () => {
      if (video.paused)
        video.play().catch(() => toast("预览视频暂不可播放", true));
      else video.pause();
    };
    video.onplay = () => ($("toggleStylePlayback").textContent = "暂停预览");
    video.onpause = () => ($("toggleStylePlayback").textContent = "播放预览");
    video.ontimeupdate = () => {
      $("stylePreviewSeek").value = video.currentTime;
      $("stylePreviewTime").textContent = time(video.currentTime * 1000);
    };
    $("stylePreviewSeek").oninput = () =>
      (video.currentTime = Number($("stylePreviewSeek").value));
    video.onloadedmetadata = () => {
      video.currentTime = Math.min(1, Math.max(0, video.duration - 0.1));
      preview();
    };
    settingsObserver?.disconnect();
    settingsObserver = new ResizeObserver(preview);
    settingsObserver.observe(frame);
    preview();
  }

  function styleFields(style = {}) {
    return (
      field(
        "size",
        "字号",
        input("styleSize", style.size || 36, 'type="number" min="18" max="72"'),
      ) +
      field(
        "height",
        `字幕高度 <output id="heightValue">${style.height || 84}%</output>`,
        input(
          "styleHeight",
          style.height || 84,
          'type="range" min="10" max="95" aria-label="字幕高度"',
        ) +
          '<span class="caption-presets"><button type="button" data-caption-height="68">68%</button><button type="button" data-caption-height="78">78%</button><button type="button" data-caption-height="88">88%</button></span>',
      ) +
      field(
        "color",
        "字幕颜色",
        input("styleColor", style.color || "#ffffff", 'type="color"'),
      ) +
      field(
        "outline",
        "描边",
        select(
          "styleOutline",
          [0, 1, 2, 3, 4, 5].map((n) => [n, n + " px"]),
          style.outline ?? 2,
        ),
      ) +
      field(
        "align",
        "字幕对齐",
        select(
          "styleAlign",
          [
            ["left", "左对齐"],
            ["center", "居中"],
            ["right", "右对齐"],
          ],
          style.align || "center",
        ),
      )
    );
  }
  function readStyle() {
    return {
      size: Number($("styleSize").value),
      height: Number($("styleHeight").value),
      color: $("styleColor").value,
      outline: Number($("styleOutline").value),
      align: $("styleAlign").value,
    };
  }
  async function openConfig(kind) {
    if (!sourceCount()) return openAssets();
    await ensureSession();
    if (!caps.languages) caps = await api("/studio/capabilities");
    const c = clone(config()[kind] || {}),
      languages = (caps.languages || []).map((l) => [l.code, l.name]);
    let body = "";
    if (kind === "clip") {
      c.method ||= session.edit_version===2 ? "cliffhanger" : "hook_first";
      if (!config().clip && session.edit_version===2) c.strategy_version=2;
      body =
        `<div class="method-grid">${Object.entries(methods)
          .map(
            ([k, v]) =>
              `<button class="method-card ${c.method === k ? "active" : ""}" data-method="${k}"><strong>${v[0]}</strong><small>${v[1]}</small></button>`,
          )
          .join("")}</div><div class="form-grid">` +
        field(
          "duration",
          "剪辑时长",
          input("duration", c.seconds ?? "", 'type="number" min="1" max="86400" placeholder="自动"'),
        ) +
        field("storyOrder","成片讲述顺序",select("storyOrder",[["episode","按集号顺叙"],["selection","按我选入的顺序"]],c.story_order||"episode")) +
        '<p id="cutExpectation" class="settings-note wide"></p>' +
        field(
          "scriptCount",
          "脚本数量",
          select(
            "scriptCount",
            [1, 2, 3].map((x) => [x, x + " 个脚本"]),
            c.script_count || 1,
          ),
        ) +
        field(
          "variantCount",
          "每份脚本裂变数量",
          select(
            "variantCount",
            [1, 2, 3, 4, 5].map((x) => [x, x + " 个版本"]),
            c.variant_count || 1,
          ),
        ) +
        field(
          "watermark",
          "文字水印（选填）",
          input(
            "watermark",
            c.watermark || "",
            'maxlength="30" placeholder="品牌或说明文字，最多 30 字"',
          ),
        ) +
        `</div><h3 class="modal-section-title">画面处理</h3><div class="checks">${Object.entries(
          effects,
        )
          .filter(([k]) => caps.effects?.includes(k))
          .map(
            ([k, name]) =>
              `<label><input type="checkbox" name="effect" value="${k}" ${(c.effects || ["basic"]).includes(k) ? "checked" : ""}>${name}</label>`,
          )
          .join(
            "",
          )}</div><div id="subtitleProtectionOptions" class="protection-settings"><label class="checks"><input id="protectSubtitles" type="checkbox" ${c.subtitle_protection?.enabled !== false ? "checked" : ""}>字幕保护</label><p class="muted">水平翻转时恢复浅色内嵌字幕的正常方向，新增字幕始终保持正常方向。</p><div class="form-grid">${field(
          "protectionMode",
          "保护区域",
          select(
            "protectionMode",
            [
              ["auto", "自动识别字幕区域"],
              ["manual", "手动调整区域"],
            ],
            c.subtitle_protection?.mode || "auto",
          ),
        )}${field("protectionTop", "上边界（距顶部 %）", input("protectionTop", c.subtitle_protection?.top ?? 55, 'type="number" min="0" max="99"'))}${field("protectionBottom", "下边界（距顶部 %）", input("protectionBottom", c.subtitle_protection?.bottom ?? 92, 'type="number" min="1" max="100"'))}</div><p class="muted">在指定区域识别字幕字形并修复翻转后的原字位置。默认识别 55%–92% 区域，可手动调整；彩色或花字字幕请检查成片。</p></div><p class="settings-note">脚本决定不同剪辑方案，裂变在相同方案上生成画面版本。热点前置会将精彩片段提前，再接回原片。</p>`;
    } else if (kind === "narration") {
      body =
        '<div class="form-grid">' +
        field(
          "language",
          "解说语种",
          select("language", languages, c.language || "zh"),
        ) +
        field(
          "seconds",
          "解说时长（秒）",
          input("seconds", c.seconds || 30, 'type="number" min="5" max="180"'),
        ) +
        field(
          "script",
          "解说脚本",
          `<textarea id="scriptText" maxlength="1600" rows="5" placeholder="可直接填写，也可以根据素材生成解说稿">${esc(c.text || "")}</textarea>`,
          "wide",
        ) +
        `<div class="modal-tools wide"><button id="generateScript">AI 生成脚本</button><button id="scriptHistory" class="quiet">脚本历史</button><span id="scriptStatus" class="muted"></span></div><label class="checks wide"><input id="speech" type="checkbox" ${c.speech !== false ? "checked" : ""} ${!caps.speech ? "disabled" : ""}>生成自然配音</label><p class="settings-note wide">按实际镜头顺序写解说，用自然语音配音，并按语音时间点对齐字幕；脚本过长时提示调整。</p>` +
        styleFields(c.style) +
        "</div>";
    } else {
      body =
        '<div class="form-grid">' +
        field(
          "sourceLanguage",
          "源语种",
          select(
            "sourceLanguage",
            [["auto", "自动识别"], ...languages],
            c.source_language || "auto",
          ),
        ) +
        field(
          "language",
          "目标语种",
          select("language", languages, c.language || "en"),
        ) +
        field(
          "subtitleMode",
          "字幕模式",
          select(
            "subtitleMode",
            [
              ["bilingual", "双语字幕"],
              ["translated", "仅译文字幕"],
            ],
            c.mode || "bilingual",
          ),
        ) +
        `<label class="checks"><input id="speech" type="checkbox" ${c.speech ? "checked" : ""} ${!caps.speech ? "disabled" : ""}>生成自然配音</label>` +
        styleFields(c.style) +
        `</div><p class="settings-note">开启配音将替换原声。画面内已有的硬字幕会保留；当前可生成新增字幕及自然配音，多角色配音暂未开放。</p>`;
    }
    if (kind !== "clip")
      body = `<div class="settings-layout"><div class="settings-fields">${body}</div>${settingsPreviewMarkup()}</div>`;
    openModal(
      { clip: "智能剪辑", narration: "影视解说", translation: "视频翻译" }[
        kind
      ],
      "配置将加入对话输入框，发送后开始制作",
      body,
      `<button id="cancelConfig" class="quiet">取消</button><button id="addConfig" class="primary">添加配置</button>`,
      kind,
    );
    $("cancelConfig").onclick = closeModal;
    if (kind === "clip") {
      const durationVisibility = () => {
        const modern=c.method==="cliffhanger" && c.strategy_version===2;
        $("duration").closest("label").hidden=c.method==="hook_first";
        $("duration").placeholder=modern?"自动，约 60～90 秒":"自动";
        $("storyOrder").closest("label").hidden=!modern;
        $("cutExpectation").textContent=modern?window.AIEditorStory.orderText(session,$("storyOrder").value)
          : c.method==="hook_first"?"精彩开头后完整连播 · "+clipDuration(c):"";
      };
      $("storyOrder").onchange=durationVisibility;
      durationVisibility();
      const protectionVisibility = () => {
        $("subtitleProtectionOptions").hidden = !document.querySelector(
          'input[name="effect"][value="mirror"]',
        ).checked;
        ["protectionTop", "protectionBottom"].forEach(
          (k) =>
            ($(k).disabled =
              $("protectionMode").value === "auto" ||
              !$("protectSubtitles").checked),
        );
      };
      document.querySelector('input[name="effect"][value="mirror"]').onchange =
        protectionVisibility;
      $("protectionMode").onchange = protectionVisibility;
      $("protectSubtitles").onchange = protectionVisibility;
      protectionVisibility();
      $("modalBody")
        .querySelectorAll("[data-method]")
        .forEach(
          (b) =>
            (b.onclick = () => {
              c.method = b.dataset.method;
              if(c.method==="cliffhanger")c.strategy_version=2;
              $("modalBody")
                .querySelectorAll("[data-method]")
                .forEach((el) => el.classList.toggle("active", el === b));
              durationVisibility();
            }),
        );
    } else {
      connectSettingsPreview(kind);
      if (kind === "narration") {
        $("generateScript").onclick = () =>
          action(async () => {
            if (analysis?.status !== "succeeded")
              throw new Error("请等待素材解析完成");
            $("scriptStatus").textContent = "正在生成…";
            const scriptSid=session.id;
            const payload={
              language: $("language").value,
              seconds: Number($("seconds").value),
              clip: config().clip,
              merge: config().merge !== false,
            };
            let d=await api("/studio/sessions/"+scriptSid+"/scripts",payload);
            if(d.status==="preparing") {
              const runId=d.run_id;
              while(session?.id===scriptSid && modalKind==="narration") {
                $("scriptStatus").textContent="正在准备与成片一致的剪辑时间线…";
                await new Promise(resolve=>setTimeout(resolve,2000));
                const latest=(await api("/studio/sessions/"+scriptSid)).item;
                const run=latest.runs.find(r=>r.id===runId);
                if(run?.status==="completed")break;
                if(run && !isActive(run.status))throw new Error(run.error||"素材或配置已更新，请重新生成");
              }
              if(session?.id!==scriptSid || modalKind!=="narration")return;
              d=await api("/studio/sessions/"+scriptSid+"/scripts",payload);
            }
            if(typeof d.text!=="string")throw new Error("剪辑配置已更新，请重新生成");
            $("scriptText").value = d.text;
            $("scriptText").dispatchEvent(new Event("input"));
            $("scriptStatus").textContent = "已保存到脚本历史";
            await refresh();
          });
        $("scriptHistory").onclick = () => {
          const list = session.messages.filter((m) => m.kind === "script");
          if (!list.length) return toast("暂无脚本历史");
          const area = document.createElement("div");
          area.innerHTML = list
            .slice()
            .reverse()
            .map(
              (m) =>
                `<button type="button" class="history-row" data-use-script="${esc(m.id)}"><strong>${esc(m.data.text.slice(0, 80))}…</strong></button>`,
            )
            .join("");
          $("scriptHistory").after(area);
          area.onclick = (e) => {
            const b = e.target.closest("[data-use-script]");
            if (b) {
              $("scriptText").value = list.find(
                (m) => m.id === b.dataset.useScript,
              ).data.text;
              $("scriptText").dispatchEvent(new Event("input"));
              area.remove();
            }
          };
        };
      }
    }
    $("addConfig").onclick = () =>
      action(async () => {
        let next;
        if (kind === "clip")
          next = {
            method: c.method,
            ...(c.method==="cliffhanger" && c.strategy_version===2?{strategy_version:2,story_order:$("storyOrder").value,
              opening_take:c.opening_take||0,ending_take:c.ending_take||0}:{}),
            seconds:
              c.method === "hook_first" || !$("duration").value
                ? null
                : Number($("duration").value),
            script_count: Number($("scriptCount").value),
            variant_count: Number($("variantCount").value),
            watermark: $("watermark").value,
            subtitle_protection: {
              enabled: $("protectSubtitles").checked,
              mode: $("protectionMode").value,
              top: Number($("protectionTop").value),
              bottom: Number($("protectionBottom").value),
            },
            effects: [
              ...document.querySelectorAll("input[name=effect]:checked"),
            ].map((e) => e.value),
          };
        else
          next = {
            language: $("language").value,
            speech: $("speech").checked && !!caps.speech,
            style: readStyle(),
            ...(kind === "narration"
              ? {
                  text: $("scriptText").value,
                  seconds: Number($("seconds").value),
                }
              : {
                  source_language: $("sourceLanguage").value,
                  mode: $("subtitleMode").value,
                }),
          };
        const nextConfig = { ...config(), [kind]: next };
        delete nextConfig.youtube_regular;
        await save({ config: nextConfig });
        closeModal();
        toast("配置已添加，点击发送开始制作");
      });
  }
  async function openHistory() {
    const data = await api("/studio/sessions");
    openModal(
      "创作历史",
      "返回先前的素材与对话",
      data.items
        .map(
          (s) =>
            `<button class="history-row" data-session="${s.id}"><strong>${esc(s.title)}</strong><small>${new Date(s.updated * 1000).toLocaleString()}</small></button>`,
        )
        .join("") || '<div class="modal-empty">暂无创作记录</div>',
      "",
      "history",
    );
  }
  async function openWorks() {
    openModal(
      "我的作品",
      "制作中的任务、已完成的成片都保存在这里；完成后可预览或直接下载。",
      `<div class="modal-tools"><input id="workSearch" placeholder="搜索作品名称"><button id="refreshWorkList">刷新</button></div><p id="worksStatus" role="status">正在读取作品…</p><div id="worksGrid" class="works-grid"></div>`,
      `<span id="workSelection">选择已完成成片可批量导出</span><button id="batchExport" class="primary">批量导出</button>`,
      "works",
    );
    const epoch = worksEpoch;
    worksFailures = 0;
    $("workSearch").oninput = drawWorks;
    $("refreshWorkList").onclick = () => action(() => readWorks(epoch), "refresh-works");
    $("batchExport").onclick = () => action(async () => {
      const ids = [...document.querySelectorAll(".work-check:checked")].map((e) => e.value);
      await exportBatch(ids);
    });
    await readWorks(epoch);
  }
  async function readWorks(epoch) {
    clearTimeout(worksTimer);
    if (modalKind !== "works" || epoch !== worksEpoch || $("worksGrid").dataset.loading) return;
    const grid = $("worksGrid");
    grid.dataset.loading = "yes";
    try {
      const data = await api("/tasks");
      if (modalKind !== "works" || epoch !== worksEpoch) return;
      allWorks = data.items;
      worksFailures = 0;
      $("worksStatus").textContent = `已完成 ${allWorks.filter((t) => t.status === "completed" && t.output).length} 条 · 制作中 ${allWorks.filter((t) => t.status === "running").length} 条 · 排队 ${allWorks.filter((t) => t.status === "queued").length} 条 · 失败 ${allWorks.filter((t) => t.status === "failed").length} 条`;
      drawWorks();
    } catch (error) {
      if (modalKind !== "works" || epoch !== worksEpoch) return;
      worksFailures++;
      $("worksStatus").textContent = "作品状态暂不可读取，正在自动重连。可点击刷新重试。";
    } finally {
      delete grid.dataset.loading;
      if (modalKind === "works" && epoch === worksEpoch && (worksFailures || allWorks.some((t) => ["queued", "running"].includes(t.status))))
        worksTimer = setTimeout(() => readWorks(epoch), document.hidden ? 10000 : worksFailures ? [4000, 8000, 15000][Math.min(2, worksFailures - 1)] : 2000);
    }
  }
  function drawWorks() {
    if (modalKind !== "works") return;
    const selected = new Set([...document.querySelectorAll(".work-check:checked")].map((e) => e.value));
    let date = "";
    $("worksGrid").innerHTML = allWorks
      .filter((t) => t.name.toLowerCase().includes($("workSearch").value.toLowerCase()))
      .map((t) => {
        const d = new Date(t.created_at * 1000).toLocaleDateString();
        const heading = d !== date ? `<h3 class="date-heading">${d}</h3>` : "";
        date = d;
        const ready = t.status === "completed" && t.output;
        const state = ready ? "制作完成" : t.status === "queued" ? "排队等待制作" : t.status === "running" ? "正在制作成片" : labels[t.status] || t.status;
        return heading + `<article class="work-card"><div class="work-cover ${ready ? "ready" : ""}">${ready ? "✓ 成片已就绪" : esc(state)}</div>${ready ? `<input type="checkbox" class="work-check" value="${t.id}" ${selected.has(t.id) ? "checked" : ""} aria-label="选择 ${esc(t.name)}">` : ""}<div class="card-body"><strong>${esc(t.name)}</strong><small>${esc(state)}${ready ? " · " + time(t.output.metadata.duration_ms) : ""}</small>${t.error_message ? `<p class="work-error">${esc(t.error_message)}</p>` : ""}<div class="work-actions">${ready ? `<button class="primary" data-work="${t.id}">预览成片</button><a class="work-download" href="${esc(t.output.download_url)}" download>下载 MP4</a>` : t.status === "failed" ? `<button data-task-retry="${t.id}">重试失败项</button>` : ""}</div></div></article>`;
      }).join("") || '<div class="modal-empty">暂无匹配作品。成片制作完成后会自动显示在这里。</div>';
  }
  async function chooseResult(task) {
    view = "result";
    if (!results.some((r) => r.id === task.id)) {
      extraResults = [task];
      results = [task, ...results];
    }
    index = finished().findIndex((t) => t.id === task.id);
    showTimeline = false;
    $("copyPanel").hidden = true;
    renderPlayer();
    renderTimeline();
  }
  async function showCopy() {
    const media = currentMedia();
    if (view !== "result" || !media) return;
    if (media.youtube_regular) {
      $("copyPanel").hidden = true;
      return;
    }
    $("copyPanel").hidden = false;
    const tid = media.id;
    const data = await api("/studio/copies/" + tid);
    if (currentMedia()?.id !== tid) return;
    copies = data.items;
    renderCopy();
  }
  function renderCopy() {
    const media = currentMedia();
    $("platformTabs").innerHTML = ["TikTok", "Facebook", "YouTube", "Instagram"]
      .map(
        (p) =>
          `<button data-platform="${p}" class="${platform === p ? "active" : ""}">${p}</button>`,
      )
      .join("");
    $("copyLabel").textContent = `描述（适用于 ${platform}）`;
    $("copyText").value = copies[platform] || "";
    $("workInfo").innerHTML =
      `${esc(media?.name)}<br>时长 ${time(media?.output?.metadata?.duration_ms)}<br><span class="muted">作品 ID：${esc(media?.id)}</span>`;
  }
  async function saveCopy(tid, p, text) {
    await api("/studio/copies/" + tid, { platform: p, text });
    if (currentMedia()?.id === tid) {
      copies[p] = text;
      $("copyState").textContent = "已保存";
    }
  }
  async function exportBatch(ids) {
    if (!ids.length) throw new Error("请选择已完成作品");
    toast("正在准备导出文件…");
    const result = await api("/studio/exports", { task_ids: ids });
    download(result.download_url);
    toast("导出文件已就绪");
  }
  function download(url) {
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.append(a);
    a.click();
    a.remove();
  }
  async function exportCurrent() {
    const media = currentMedia();
    if (!media?.output) return;
    openModal(
      "导出作品",
      "下载当前作品，或批量下载本次完成的视频",
      `<p>${esc(media.name)}</p><p class="muted">MP4 · ${time(media.output.metadata.duration_ms)} · 含已选字幕和画面处理</p>`,
      `<button id="exportAll">导出全部 ${finished().length} 条</button><button id="exportOne" class="primary">下载当前视频</button>`,
      "export",
    );
    $("exportOne").onclick = () => {
      download(media.output.download_url);
      closeModal();
    };
    $("exportAll").onclick = () =>
      action(async () => {
        await exportBatch(finished().map((t) => t.id));
        closeModal();
      });
  }
  async function handle(name) {
    if(name==="story-plan")return openStory();
    if (name === "youtube_regular") {
      const configure = () =>
        window.YoutubeRegular.configure({
          assets,
          current: config().youtube_regular,
          openModal,
          closeModal,
          esc,
          action,
          save: async (youtube_regular) => {
            await save({ config: { youtube_regular } });
            toast("已设置普通视频，点击发送开始制作");
          },
        });
      if (!assets.length) return openAssets(configure);
      return configure();
    }
    if (name === "regular-trim") {
      const task = currentMedia();
      if (!task || !task.youtube_regular || view !== "result") return;
      return window.YoutubeRegular.refine({
        task,
        api,
        openModal,
        closeModal,
        action,
        esc,
        toast,
        openOriginal: chooseResult,
        refresh: session ? refresh : openWorks,
      });
    }
    if (name === "regular-intake") {
      const task = currentMedia();
      if (view === "result" && task?.youtube_regular)
        return window.YoutubeMaterial.open(task.id);
      return;
    }
    if (["clip", "narration", "translation"].includes(name))
      return openConfig(name);
    if (name === "assets") return openAssets();
    if (name === "analysis") return analyze();
    if (name === "history") return openHistory();
    if (name === "works") return openWorks();
    if (name === "new") {
      clearTimeout(timer);
      ++navigationEpoch;
      clearTimeout(timer);
      sourceProgress = null;
      progressEtag = "";
      progressRevision = "";
      session = null;
      assets = [];
      analysis = null;
      results = [];
      extraResults = [];
      index = 0;
      view = "source";
      showTimeline = false;
      location.hash = "";
      $("copyPanel").hidden = true;
      $("prompt").value = "";
      messageKey = "";
      requestReceipt = null;
      render();
      return;
    }
    if (name === "source-view") {
      view = "source";
      index = 0;
      $("copyPanel").hidden = true;
      renderPlayer();
      renderTimeline();
      return;
    }
    if (name === "result-view") {
      if (!finished().length) return toast("作品完成后会显示在这里");
      view = "result";
      index = 0;
      $("copyPanel").hidden = true;
      renderPlayer();
      renderTimeline();
      return;
    }
    if (name === "show-copy") return showCopy();
    if (name === "hide-timeline") {
      showTimeline = false;
      renderTimeline();
      return;
    }
    if (name === "close-copy") {
      $("copyPanel").hidden = true;
      return;
    }
    if (name === "export") return exportCurrent();
    if (name === "prompts")
      return openModal(
        "常用提示词",
        "点击填入对话，可编辑后发送",
        [
          "请概括视频剧情并指出重要转折。",
          "哪一段最适合做短视频开场？",
          "请用爆点引流方式剪辑视频。",
          "提供一份适合 TikTok 的短剧剪辑方案。",
          "提供一份适合 YouTube Shorts 的剪辑方案。",
          "高燃剪辑并翻译成英文。",
          "哪些画面体现了人物情绪变化？",
        ]
          .map(
            (p) =>
              `<button class="history-row" data-prompt="${esc(p)}"><strong>${esc(p)}</strong><span>↗</span></button>`,
          )
          .join(""),
        "",
        "prompts",
      );
    if (name === "generate-copy") {
      const tid = currentMedia()?.id,
        p = platform;
      if (!tid) return;
      $("copyState").textContent = "正在生成…";
      const d = await api("/studio/copies/" + tid, { platform: p });
      if (currentMedia()?.id === tid) {
        copies[p] = d.text;
        renderCopy();
        $("copyState").textContent = "已生成并保存";
      }
      return;
    }
    if (name === "copy-text") {
      const text = $("copyText").value;
      await saveCopy(currentMedia().id, platform, text);
      await navigator.clipboard.writeText(text);
      toast("文案已复制");
    }
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.disabled) return;
    if (b.dataset.action) {
      e.preventDefault();
      return action(
        () => handle(b.dataset.action),
        "action:" + b.dataset.action,
      );
    }
    if (b.hasAttribute("data-prompt")) {
      $("prompt").value = b.dataset.prompt;
      if (modalKind === "prompts") closeModal();
      $("prompt").focus();
      return;
    }
    if (b.dataset.asset) {
      const item = library.find((a) => a.id === b.dataset.asset);
      if (item)
        try {
          sourceSelection = Sources.toggle(sourceSelection, {
            kind: "asset",
            asset_id: item.id,
            name: item.name,
          });
          drawSelection();
        } catch (error) {
          toast(error.message, true);
        }
      return;
    }
    if (b.hasAttribute("data-move-source")) {
      sourceSelection = Sources.move(
        sourceSelection,
        Number(b.dataset.moveSource),
        Number(b.dataset.dir),
      );
      drawSelection();
      return;
    }
    if (b.hasAttribute("data-remove-source")) {
      sourceSelection = sourceSelection.filter(
        (_, i) => i !== Number(b.dataset.removeSource),
      );
      drawSelection();
      return;
    }
    if (b.dataset.sourceRetry)
      return action(async () => {
        await api(
          "/studio/sessions/" +
            session.id +
            "/selection/items/" +
            b.dataset.sourceRetry +
            "/retry",
          {},
        );
        await refresh();
      }, "source-retry:" + b.dataset.sourceRetry);
    if (b.dataset.previewRetry)
      return action(async () => {
        await api("/assets/" + b.dataset.previewRetry + "/preview", {
          retry: true,
        });
        await refresh();
      }, "preview-retry:" + b.dataset.previewRetry);
    if (b.dataset.sourceRemove)
      return action(async () => {
        const remaining = session.selection.items
          .filter((i) => i.id !== b.dataset.sourceRemove)
          .map(Sources.intent);
        if (!remaining.length)
          throw new Error("至少保留一个素材；也可以重试当前素材");
        await openAssets();
        sourceSelection = remaining;
        drawSelection();
        toast("已从草稿移除该素材，确认新选择后继续制作");
      }, "source-remove");
    if (b.dataset.readySource) {
      index = assets.findIndex((a) => a.id === b.dataset.readySource);
      view = "source";
      renderPlayer();
      renderTimeline();
      return;
    }
    if (b.dataset.removeConfig)
      return action(async () => {
        const c = clone(config());
        delete c[b.dataset.removeConfig];
        await save({ config: c });
      });
    if (b.hasAttribute("data-media"))
      return action(async () => {
        if (Number(b.dataset.media) < 0) return;
        index = Number(b.dataset.media);
        renderPlayer();
        renderTimeline();
        if (view === "result" && !$("copyPanel").hidden) await showCopy();
      });
    if (b.hasAttribute("data-seek")) {
      $("player").currentTime = Number(b.dataset.seek) / 1000;
      $("player")
        .play()
        .catch(() => {});
      return;
    }
    if (b.dataset.session)
      return action(async () => {
        closeModal();
        await loadSession(b.dataset.session);
      });
    if (b.dataset.runResults || b.dataset.runDownload)
      return action(async () => {
        const run = session?.runs.find((r) => r.id === (b.dataset.runResults || b.dataset.runDownload));
        const ready = (run?.tasks || []).filter((t) => t.status === "completed" && t.output);
        if (!ready.length) return toast("成片尚未就绪，状态将自动更新");
        if (b.dataset.runDownload) {
          if (ready.length === 1) download(ready[0].output.download_url);
          else await exportBatch(ready.map((t) => t.id));
        } else await chooseResult(ready[0]);
      }, b.dataset.runDownload ? "download-run" : "view-run");
    if (b.dataset.work)
      return action(async () => {
        const t = allWorks.find((x) => x.id === b.dataset.work);
        closeModal();
        await chooseResult(t);
      });
    if (b.dataset.platform) {
      platform = b.dataset.platform;
      renderCopy();
      return;
    }
    if (b.dataset.retry)
      return action(async () => {
        await api("/studio/runs/" + b.dataset.retry + "/retry", {});
        await refresh();
      });
    if (b.dataset.taskRetry)
      return action(async () => {
        await api("/tasks/" + b.dataset.taskRetry + "/retry", {});
        await openWorks();
      });
    if (b.dataset.suggestion)
      return action(async () => {
        const m = session.messages.find((m) => m.id === b.dataset.suggestion);
        await save({ config: m.data.config });
        await openConfig("clip");
      });
    if (b.dataset.script)
      return action(async () => {
        const m = session.messages.find((m) => m.id === b.dataset.script);
        await save({
          config: {
            ...config(),
            narration: {
              language: m.data.language || "zh",
              text: m.data.text,
              seconds: 30,
              speech: !!caps.speech,
            },
          },
        });
        await openConfig("narration");
      });
    if (b.dataset.cite) {
      const m = session.messages.find((m) => m.id === b.dataset.cite),
        ref = m.data.evidence[Number(b.dataset.citeIndex)];
      view = "source";
      index = assets.findIndex((a) => a.id === ref.source_id);
      showTimeline = true;
      $("copyPanel").hidden = true;
      renderPlayer();
      renderTimeline();
      const jump = () => {
        $("player").currentTime = ref.start_ms / 1000;
        $("player")
          .play()
          .catch(() => {});
      };
      if ($("player").readyState >= 1) jump();
      else $("player").addEventListener("loadedmetadata", jump, { once: true });
    }
  });
  $("closeModal").onclick = closeModal;
  $("modal").addEventListener("cancel", (e) => {
    if (uploading) e.preventDefault();
  });
  $("modal").addEventListener("close", () => {
    window.AIEditorStory.close();
    $("captionPreview").hidden = true;
    $("stylePreviewVideo")?.pause();
    settingsObserver?.disconnect();
    modalKind = "";
    clearTimeout(worksTimer);
    ++worksEpoch;
    ++libraryEpoch;
    catalogController?.abort();
    assetController?.abort();
  });
  $("composer").onsubmit = (e) => {
    e.preventDefault();
    action(send, "send");
  };
  $("prompt").onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      action(send, "send");
    }
  };
  $("player").addEventListener("timeupdate", updatePlayhead);
  $("player").addEventListener("error", () => {
    if (mediaKey) toast("视频暂不可播放，请刷新作品或素材后重试", true);
  });
  $("copyText").oninput = () => {
    clearTimeout(copySaveTimer);
    const tid = currentMedia()?.id,
      p = platform,
      text = $("copyText").value;
    if (tid) {
      copies[p] = text;
      $("copyState").textContent = "保存中…";
      copySaveTimer = setTimeout(
        () => saveCopy(tid, p, text).catch((e) => toast(e.message, true)),
        800,
      );
    }
  };
  window.addEventListener("beforeunload", (e) => {
    if (uploading) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && session) refresh(false);
    else queueRefresh();
    if (!document.hidden && modalKind === "works") readWorks(worksEpoch);
  });
  window.addEventListener("online", () => {
    if (session) refresh(false);
    if (modalKind === "works") readWorks(worksEpoch);
  });
  $("previewQuality").onchange = () => {
    const player = $("player"),
      position = player.currentTime,
      playing = !player.paused;
    previewQuality = $("previewQuality").value;
    player.dataset.identity = "";
    renderPlayer();
    const identity=player.dataset.identity;
    player.addEventListener(
      "loadedmetadata",
      () => {
        if(player.dataset.identity!==identity)return;
        player.currentTime = Math.min(position, player.duration || position);
        if (playing) player.play().catch(() => {});
      },
      { once: true },
    );
  };
  $("player").addEventListener("waiting", () => {
    $("playerLoading").hidden = false;
  });
  $("player").addEventListener("loadeddata", () => {
    $("playerLoading").hidden = true;
    const start=Number($("player").dataset.loadStarted);
    if(start) {
      $("player").dataset.firstFrameMs=String(Math.round(performance.now()-start));
      performance.measure("editor-first-frame:"+$("player").dataset.identity,{start});
    }
  });
  $("player").addEventListener("playing", () => {
    $("playerLoading").hidden = true;
  });
  $("player").addEventListener("error", () => {
    $("playerLoading").hidden = false;
    $("playerLoading").textContent = "视频暂不可播放，可切换原片或重新选择素材";
  });
  $("openNavigation").onclick = () => {
    $("navigationPanel").classList.add("is-open");
    $("openNavigation").setAttribute("aria-expanded", "true");
  };
  $("closeNavigation").onclick = () => {
    $("navigationPanel").classList.remove("is-open");
    $("openNavigation").setAttribute("aria-expanded", "false");
  };
  async function init() {
    iconify(document);
    auth = await api("/api/ui/topbar");
    window.UiTopbar.render({
      auth,
      userCard: "#userCard",
      authButton: "#authButton",
      refreshButton: "#refreshButton",
    });
    $("authButton").onclick = () => window.UiTopbar.handleAuthAction({ auth });
    window.QuickNav.render({
      container: "#quickNav",
      auth,
      activeKey: "videoEditingAi",
    }).catch(() => {});
    if (!auth.authenticated) {
      $("gate").textContent = "请登录后进入智能剪辑工作台";
      return;
    }
    api("/studio/capabilities")
      .then((data) => {
        caps = data;
      })
      .catch((e) => toast(e.message, true));
    $("gate").hidden = true;
    $("workspace").hidden = false;
    let pending;
    try {
      pending = JSON.parse(sessionStorage.getItem(pendingKey()) || "null");
    } catch {}
    if (pending) {
      try {
        const receipt = (
          await api(
            "/studio/selection-operations?" +
              new URLSearchParams({ operation_id: pending.body.operation_id }),
          )
        ).receipt;
        if (receipt) {
          sessionStorage.removeItem(pendingKey());
          if (!/^#ss_[a-f0-9]{32}$/.test(location.hash))
            location.hash = receipt.session_id;
        }
      } catch {
        toast("上次选择回执暂时无法核对，选择草稿仍已保留", true);
      }
    }
    if (/^#ss_[a-f0-9]{32}$/.test(location.hash))
      await loadSession(location.hash.slice(1));
    else render();
  }
  init().catch((e) => {
    $("gate").hidden = false;
    $("gate").textContent = "工作台暂时无法打开：" + e.message;
  });
})();
