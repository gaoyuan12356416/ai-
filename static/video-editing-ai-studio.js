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
    hook_first: ["热点前置", "精彩开场，接回完整叙事"],
    highlight_montage: ["高燃混剪", "串联亮点，紧凑呈现"],
    golden_three: ["黄金三段", "开场 · 剧情 · 悬念"],
    cliffhanger: ["爆点引流", "铺垫冲突，留下悬念"],
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
    analyzing: "正在解析素材",
    preparing: "正在生成脚本与字幕",
    rendering: "正在制作成片",
    running: "制作中",
    completed: "已完成",
    failed: "制作失败",
    succeeded: "解析完成",
  };
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
    ["queued", "analyzing", "preparing", "rendering"].includes(s);
  const config = () => session?.config || {};
  const finished = () => results.filter((x) => x.status === "completed");
  async function api(path, body, method) {
    const response = await fetch(path.startsWith("/api/") ? path : API + path, {
      method: method || (body === undefined ? "GET" : "POST"),
      credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response
      .json()
      .catch(() => ({ error: "服务未返回有效响应" }));
    if (!response.ok)
      throw new Error(data.error || data.message || "请求未完成");
    return data;
  }
  function toast(text, error = false) {
    clearTimeout(toastTimer);
    $("toast").textContent = text;
    $("toast").classList.toggle("error", error);
    $("toast").hidden = false;
    toastTimer = setTimeout(() => ($("toast").hidden = true), 6500);
  }
  async function action(fn) {
    if (busy) return;
    busy = true;
    $("send").disabled = true;
    try {
      return await fn();
    } catch (e) {
      toast(e.message, true);
    } finally {
      busy = false;
      updateComposer();
    }
  }
  function openModal(title, subtitle, body, footer = "", kind = "") {
    $("modalTitle").textContent = title;
    $("modalSubtitle").textContent = subtitle;
    $("modalBody").innerHTML = body;
    $("modalFooter").innerHTML = footer;
    modalKind = kind;
    iconify($("modal"));
    if (!$("modal").open) $("modal").showModal();
  }
  function closeModal() {
    if (uploading) {
      toast("文件正在上传，请稍候");
      return;
    }
    $("modal").close();
    $("captionPreview").hidden = true;
    modalKind = "";
  }
  async function ensureSession() {
    if (!session) {
      session = (await api("/studio/sessions", { source_ids: [] })).item;
      location.hash = session.id;
    }
    return session;
  }
  async function save(change) {
    await ensureSession();
    session = (
      await api(
        "/studio/sessions/" + session.id,
        { ...change, version: session.version },
        "PATCH",
      )
    ).item;
    assets = session.assets;
    render();
  }
  async function loadSession(id) {
    const item = (await api("/studio/sessions/" + encodeURIComponent(id))).item;
    session = item;
    assets = item.assets;
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
    await refresh();
  }
  async function refresh() {
    clearTimeout(timer);
    if (!session) return;
    const sid = session.id;
    try {
      const item = (await api("/studio/sessions/" + sid)).item;
      if (session?.id !== sid) return;
      const oldCompleted = new Set(finished().map((r) => r.id));
      session = item;
      assets = item.assets;
      results = [
        ...item.runs.flatMap((r) => r.tasks),
        ...extraResults.filter(
          (x) => !item.runs.some((r) => r.tasks.some((t) => t.id === x.id)),
        ),
      ];
      pollFailures = 0;
      if (item.analysis_id) {
        const a = await api("/analyses/" + item.analysis_id);
        if (session?.id !== sid) return;
        analysis = a;
      }
      const newlyCompleted = finished().find((t) => !oldCompleted.has(t.id));
      if (newlyCompleted && !$("modal").open) {
        view = "result";
        index = finished().findIndex((t) => t.id === newlyCompleted.id);
        showTimeline = false;
      }
      render();
      if (newlyCompleted && !$("modal").open) await showCopy();
    } catch (e) {
      pollFailures++;
      if (pollFailures === 1)
        toast("进度暂不可读取，将自动重连。" + e.message, true);
    }
    if (session?.id === sid)
      timer = setTimeout(
        refresh,
        document.hidden ? 20000 : Math.min(20000, 4000 * (1 + pollFailures)),
      );
  }
  function configSummary(c) {
    return `<div class="configuration-summary"><strong>${c.clip ? esc(methods[c.clip.method]?.[0]) : "视频创作"}${c.narration ? " · 影视解说" : ""}${c.translation ? " · 视频翻译" : ""}</strong><div class="summary-grid"><div><span>素材处理</span>${c.merge === false ? "逐条处理" : "合并素材"}</div><div><span>输出数量</span>${(c.merge === false ? assets.length : 1) * (c.clip?.script_count || 1) * (c.clip?.variant_count || 1)} 条</div>${c.clip ? `<div><span>目标时长</span>${c.clip.seconds ? c.clip.seconds + " 秒" : "自动"}</div><div><span>脚本 × 裂变</span>${c.clip.script_count} × ${c.clip.variant_count}</div>` : ""}${c.translation ? `<div><span>翻译语种</span>${esc(languageName(c.translation.language))}</div><div><span>字幕</span>${c.translation.mode === "bilingual" ? "双语" : "仅译文"}</div>` : ""}</div></div>`;
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
      .filter((r) => r.status !== "completed")
      .map(
        (r) =>
          `<div class="run-card"><header><strong>${esc(labels[r.status])}</strong><span>${r.tasks.filter((t) => t.status === "completed").length}${r.tasks.length ? " / " + r.tasks.length : ""}</span></header>${r.status === "failed" ? `<p class="muted">${esc(r.error)}</p><button data-retry="${r.id}">重试失败项</button>` : `<progress ${r.status === "rendering" ? `max="${Math.max(1, r.tasks.length)}" value="${r.tasks.filter((t) => t.status === "completed").length}"` : ""}></progress><small>任务已保存，离开页面后会继续处理</small>`}</div>`,
      )
      .join("");
    box.innerHTML = html;
    if (nearEnd || !entries.length) box.scrollTop = box.scrollHeight;
  }
  function updateComposer() {
    const c = config();
    $("configChips").innerHTML = ["clip", "narration", "translation"]
      .filter((k) => c[k])
      .map(
        (k) =>
          `<span class="chip"><button type="button" data-action="${k}">${{ clip: "智能剪辑", narration: "影视解说", translation: "视频翻译" }[k]}</button><button type="button" data-remove-config="${k}" aria-label="移除${k}">×</button></span>`,
      )
      .join("");
    const configured = !!(c.clip || c.narration || c.translation),
      count =
        (c.merge === false ? assets.length : 1) *
        (c.clip?.script_count || 1) *
        (c.clip?.variant_count || 1);
    $("submitHint").textContent = busy
      ? "正在处理…"
      : !assets.length
        ? "先选择素材，开始创作"
        : configured
          ? `预计 ${count} 条作品 · 发送开始制作`
          : "可提问，或添加创作配置";
    $("send").disabled = busy || !assets.length;
  }
  function currentMedia() {
    return (view === "source" ? assets : finished())[index];
  }
  function renderPlayer() {
    const list = view === "source" ? assets : finished();
    if (index >= list.length) index = 0;
    const media = list[index],
      url = view === "source" ? media?.preview_url : media?.output?.preview_url;
    if (url !== mediaKey) {
      mediaKey = url || "";
      const player = $("player");
      player.pause();
      if (url) player.src = url;
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
    $("stripLabel").innerHTML =
      view === "source" ? "待处理<br>视频" : "已完成<br>作品";
    const stripKey = JSON.stringify([view, index, list.map((m) => m.id)]);
    if ($("filmstrip").dataset.key !== stripKey) {
      $("filmstrip").dataset.key = stripKey;
      $("filmstrip").innerHTML = list
        .map(
          (m, i) =>
            `<button class="thumbnail ${index === i ? "active" : ""}" data-media="${i}" aria-label="${view === "source" ? "素材" : "作品"} ${i + 1}：${esc(m.name)}"><video src="${esc(view === "source" ? m.preview_url : m.output.preview_url)}#t=0.1" preload="metadata" muted playsinline></video><span class="number">${i + 1}</span><span class="duration">${time(m.duration_ms || m.output?.metadata?.duration_ms)}</span></button>`,
        )
        .join("");
    }
    $("stageSuggestions").hidden = !!assets.length;
    const active = session?.runs.find(
      (r) => isActive(r.status) && r.kind === "create",
    );
    $("renderOverlay").hidden = !active;
    if (active)
      $("renderOverlay").innerHTML =
        `${esc(labels[active.status])} · ${active.tasks.filter((t) => t.status === "completed").length} / ${active.tasks.length || "—"} 条<progress ${active.status === "rendering" ? `max="${Math.max(1, active.tasks.length)}" value="${active.tasks.filter((t) => t.status === "completed").length}"` : ""}></progress>`;
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
    $("sessionState").textContent = assets.length
      ? `已选 ${assets.length} 个素材 · ${analysis?.status === "succeeded" ? "解析完成" : "随时添加创作配置"}`
      : "和 AI 一起完成你的下一条视频";
    renderMessages();
    updateComposer();
    renderPlayer();
    renderTimeline();
  }
  async function analyze() {
    if (!assets.length) return openAssets();
    await ensureSession();
    await api("/studio/sessions/" + session.id + "/analyze", {
      config: config(),
      operation_id: op(),
    });
    showTimeline = true;
    view = "source";
    await refresh();
  }
  async function send() {
    if (!assets.length) return openAssets();
    const text = $("prompt").value.trim(),
      c = config();
    await ensureSession();
    if (!text && (c.clip || c.narration || c.translation)) {
      const signature = JSON.stringify([session.id, c]);
      if (!requestReceipt || requestReceipt.signature !== signature)
        requestReceipt = { signature, operation: op() };
      await api("/studio/sessions/" + session.id + "/submit", {
        config: c,
        operation_id: requestReceipt.operation,
      });
      requestReceipt = null;
      $("prompt").value = "";
      toast("已提交，云端将持续制作");
      await refresh();
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
  async function openAssets() {
    sourceSelection = assets.map((a) => a.id);
    openModal(
      "媒资库",
      "选择一个或多个视频，按选入顺序组合创作",
      `<div class="modal-tools"><input id="assetQuery" type="search" placeholder="搜索素材或剧名"><button id="searchAssets">查询</button><button id="toggleUpload" class="primary" data-icon="plus">上传视频</button></div><div id="uploadZone" class="upload-zone" hidden><input id="uploadFile" type="file" accept=".mp4,.mov" multiple><div class="form-grid" style="margin-top:12px"><label class="field">剧名（选填）<input id="uploadSeries" maxlength="80" placeholder="同名剧目归为一组"></label><label class="field">起始集号（选填）<input id="uploadEpisode" type="number" min="1" max="100000"></label></div><p>支持 MP4、MOV，每个文件最多 1 GiB。上传后可在自己的媒资库中重复使用。</p><progress id="uploadProgress" max="100" value="0" hidden></progress><p id="uploadStatus"></p><button id="uploadStart">开始上传</button></div><div id="assetGrid" class="asset-grid"></div><div id="selectionTray" class="selection-tray"></div>`,
      `<label class="checks"><input type="checkbox" id="mergeSources" ${config().merge !== false ? "checked" : ""}> 合并所选素材</label><span id="assetCount">已选 ${sourceSelection.length} 个</span><button id="confirmAssets" class="primary">确认选择</button>`,
      "assets",
    );
    $("toggleUpload").onclick = () =>
      ($("uploadZone").hidden = !$("uploadZone").hidden);
    $("searchAssets").onclick = () => action(readAssets);
    $("assetQuery").onkeydown = (e) => {
      if (e.key === "Enter") action(readAssets);
    };
    $("uploadStart").onclick = () => action(uploadFiles);
    $("confirmAssets").onclick = () =>
      action(async () => {
        if (!sourceSelection.length) throw new Error("请至少选择一个素材");
        const same =
          JSON.stringify(sourceSelection) ===
          JSON.stringify(assets.map((x) => x.id));
        await save({
          source_ids: sourceSelection,
          config: { ...config(), merge: $("mergeSources").checked },
        });
        view = "source";
        index = 0;
        closeModal();
        if (!same || !analysis) await analyze();
        else render();
      });
    await readAssets();
  }
  async function readAssets() {
    const data = await api(
      "/assets?" +
        new URLSearchParams({ q: $("assetQuery").value, limit: 200 }),
    );
    library = data.items;
    drawAssets();
  }
  function drawAssets() {
    const pool = [...assets, ...library];
    $("assetGrid").innerHTML =
      library
        .map(
          (a) =>
            `<button class="asset-card ${sourceSelection.includes(a.id) ? "selected" : ""}" data-asset="${a.id}"><video preload="metadata" muted src="${esc(a.preview_url)}#t=0.1"></video><span class="card-check">${sourceSelection.includes(a.id) ? sourceSelection.indexOf(a.id) + 1 : "+"}</span><span class="card-body"><strong>${esc(a.name)}</strong><small>${esc(a.series_code || "本地视频")} · ${time(a.duration_ms)}</small></span></button>`,
        )
        .join("") ||
      '<div class="modal-empty">暂无素材，上传视频即可开始</div>';
    $("selectionTray").innerHTML = sourceSelection
      .map(
        (id, i) =>
          `<div class="selection-item">${i + 1}. ${esc(pool.find((a) => a.id === id)?.name || "已选素材")}<button data-move-source="${i}" data-dir="-1" ${i === 0 ? "disabled" : ""} aria-label="上移">↑</button><button data-move-source="${i}" data-dir="1" ${i === sourceSelection.length - 1 ? "disabled" : ""} aria-label="下移">↓</button><button data-remove-source="${id}" aria-label="移除素材">×</button></div>`,
      )
      .join("");
    $("assetCount").textContent = "已选 " + sourceSelection.length + " 个";
  }
  async function uploadFiles() {
    const files = Array.from($("uploadFile").files);
    if (!files.length) throw new Error("请选择文件");
    uploading = true;
    $("uploadProgress").hidden = false;
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        if (!/\.(mp4|mov)$/i.test(file.name) || file.size > 1024 ** 3)
          throw new Error("请选择不超过 1 GiB 的 MP4 或 MOV");
        $("uploadStatus").textContent =
          `正在上传 ${i + 1}/${files.length}：${file.name}`;
        const data = await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open(
            "POST",
            API +
              "/assets?" +
              new URLSearchParams({
                name: file.name,
                series: $("uploadSeries").value,
                episode: $("uploadEpisode").value
                  ? Number($("uploadEpisode").value) + i
                  : 0,
              }),
          );
          xhr.setRequestHeader("Content-Type", "application/octet-stream");
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable)
              $("uploadProgress").value = (e.loaded / e.total) * 100;
          };
          xhr.onerror = () =>
            reject(new Error("上传连接中断，请查询媒资库后重试"));
          xhr.onload = () => {
            let d;
            try {
              d = JSON.parse(xhr.responseText);
            } catch {
              return reject(new Error("上传结果无法读取，请查询媒资库"));
            }
            xhr.status < 300
              ? resolve(d)
              : reject(new Error(d.error || "上传未完成"));
          };
          xhr.send(file);
        });
        if (!sourceSelection.includes(data.item.id))
          sourceSelection.push(data.item.id);
      }
      $("uploadStatus").textContent = "上传完成，已自动选中";
      await readAssets();
    } finally {
      uploading = false;
    }
  }
  const field = (name, label, control, extra = "") =>
    `<label class="field ${extra}">${label}${control}</label>`;
  const input = (id, value, attrs = "") =>
    `<input id="${id}" value="${esc(value)}" ${attrs}>`;
  const select = (id, options, value) =>
    `<select id="${id}">${options.map(([v, n]) => `<option value="${esc(v)}" ${String(v) === String(value) ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>`;
  function styleFields(style = {}) {
    return (
      field(
        "size",
        "字号",
        input("styleSize", style.size || 36, 'type="number" min="18" max="72"'),
      ) +
      field(
        "height",
        "字幕高度（%）",
        input(
          "styleHeight",
          style.height || 84,
          'type="range" min="10" max="95"',
        ),
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
    if (!assets.length) return openAssets();
    await ensureSession();
    caps = await api(
      "/studio/capabilities?seconds=" +
        Math.round(assets.reduce((n, a) => n + a.duration_ms, 0) / 1000),
    );
    const c = clone(config()[kind] || {}),
      languages = (caps.languages || []).map((l) => [l.code, l.name]);
    let body = "";
    if (kind === "clip") {
      c.method ||= "hook_first";
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
          select(
            "duration",
            [
              ["", "自动选择"],
              ...(caps.durations || []).map((x) => [
                x.seconds,
                `${x.min}–${x.max} 秒`,
              ]),
            ],
            c.seconds ?? "",
          ),
        ) +
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
          )}</div><p class="settings-note">脚本决定不同剪辑方案，裂变在相同方案上生成画面版本。热点前置会将精彩片段提前，再接回原片。</p>`;
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
        `<div class="modal-tools wide"><button id="generateScript">AI 生成脚本</button><button id="scriptHistory" class="quiet">脚本历史</button><span id="scriptStatus" class="muted"></span></div><label class="checks wide"><input id="speech" type="checkbox" ${c.speech !== false ? "checked" : ""} ${!caps.speech ? "disabled" : ""}>生成标准配音</label><p class="settings-note wide">解说稿会按成片时长生成字幕与配音。可先校对脚本，再添加配置。</p>` +
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
        `<label class="checks"><input id="speech" type="checkbox" ${c.speech ? "checked" : ""} ${!caps.speech ? "disabled" : ""}>生成标准配音</label>` +
        styleFields(c.style) +
        `</div><p class="settings-note">开启配音将替换原声。画面内已有的硬字幕会保留；当前可生成新增字幕及标准配音，多角色配音暂未开放。</p>`;
    }
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
      const durationVisibility = () =>
        ($("duration").closest("label").hidden = c.method === "hook_first");
      durationVisibility();
      $("modalBody")
        .querySelectorAll("[data-method]")
        .forEach(
          (b) =>
            (b.onclick = () => {
              c.method = b.dataset.method;
              $("modalBody")
                .querySelectorAll("[data-method]")
                .forEach((el) => el.classList.toggle("active", el === b));
              durationVisibility();
            }),
        );
    } else {
      const preview = () => {
        const s = readStyle();
        $("captionPreview").textContent =
          kind === "translation"
            ? "让精彩故事跨越语言\nLet great stories travel."
            : "这是一段解说字幕预览";
        $("captionPreview").hidden = false;
        $("captionPreview").style.bottom = 100 - s.height + "%";
        $("captionPreview").style.fontSize = Math.max(12, s.size * 0.65) + "px";
        $("captionPreview").style.color = s.color;
        $("captionPreview").style.textAlign = s.align;
      };
      [
        "styleSize",
        "styleHeight",
        "styleColor",
        "styleAlign",
        "styleOutline",
      ].forEach((k) => ($(k).oninput = preview));
      if (kind === "narration") {
        $("generateScript").onclick = () =>
          action(async () => {
            if (analysis?.status !== "succeeded")
              throw new Error("请等待素材解析完成");
            $("scriptStatus").textContent = "正在生成…";
            const d = await api("/studio/sessions/" + session.id + "/scripts", {
              language: $("language").value,
              seconds: Number($("seconds").value),
            });
            $("scriptText").value = d.text;
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
            seconds:
              c.method === "hook_first" || !$("duration").value
                ? null
                : Number($("duration").value),
            script_count: Number($("scriptCount").value),
            variant_count: Number($("variantCount").value),
            watermark: $("watermark").value,
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
        await save({ config: { ...config(), [kind]: next } });
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
    allWorks = (await api("/tasks")).items;
    openModal(
      "我的作品",
      "制作中的任务、已完成的作品都保存在这里",
      `<div class="modal-tools"><input id="workSearch" placeholder="搜索作品名称"><button id="refreshWorkList">刷新</button></div><div id="worksGrid" class="works-grid"></div>`,
      `<span id="workSelection">选择已完成作品可批量导出</span><button id="batchExport" class="primary">批量导出</button>`,
      "works",
    );
    $("workSearch").oninput = drawWorks;
    $("refreshWorkList").onclick = () => action(openWorks);
    $("batchExport").onclick = () =>
      action(async () => {
        const ids = [...document.querySelectorAll(".work-check:checked")].map(
          (e) => e.value,
        );
        await exportBatch(ids);
      });
    drawWorks();
  }
  function drawWorks() {
    let date = "";
    $("worksGrid").innerHTML =
      allWorks
        .filter((t) =>
          t.name.toLowerCase().includes($("workSearch").value.toLowerCase()),
        )
        .map((t) => {
          const d = new Date(t.created_at * 1000).toLocaleDateString();
          const heading =
            d !== date ? `<h3 class="date-heading">${d}</h3>` : "";
          date = d;
          return (
            heading +
            `<article class="work-card">${t.output ? `<video preload="metadata" muted src="${esc(t.output.preview_url)}#t=0.1"></video><input type="checkbox" class="work-check" value="${t.id}" aria-label="选择 ${esc(t.name)}">` : '<div class="modal-empty">' + esc(labels[t.status] || t.status) + "</div>"}<div class="card-body"><strong>${esc(t.name)}</strong><small>${esc(labels[t.status] || t.status)}${t.output ? " · " + time(t.output.metadata.duration_ms) : ""}</small>${t.output ? `<button data-work="${t.id}">预览作品</button>` : t.status === "failed" ? `<button data-task-retry="${t.id}">重试</button>` : ""}</div></article>`
          );
        })
        .join("") || '<div class="modal-empty">暂无匹配作品</div>';
  }
  async function chooseResult(task) {
    view = "result";
    if (!results.some((r) => r.id === task.id)) {
      extraResults = [task];
      results = [task, ...results];
    }
    index = finished().findIndex((t) => t.id === task.id);
    showTimeline = false;
    renderPlayer();
    renderTimeline();
    await showCopy();
  }
  async function showCopy() {
    const media = currentMedia();
    if (view !== "result" || !media) return;
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
    if (["clip", "narration", "translation"].includes(name))
      return openConfig(name);
    if (name === "assets") return openAssets();
    if (name === "analysis") return analyze();
    if (name === "history") return openHistory();
    if (name === "works") return openWorks();
    if (name === "new") {
      clearTimeout(timer);
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
      renderPlayer();
      renderTimeline();
      await showCopy();
      return;
    }
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
          "请用热点前置方式剪辑视频。",
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
      return action(() => handle(b.dataset.action));
    }
    if (b.hasAttribute("data-prompt")) {
      $("prompt").value = b.dataset.prompt;
      if (modalKind === "prompts") closeModal();
      $("prompt").focus();
      return;
    }
    if (b.dataset.asset) {
      const id = b.dataset.asset;
      sourceSelection = sourceSelection.includes(id)
        ? sourceSelection.filter((x) => x !== id)
        : [...sourceSelection, id];
      drawAssets();
      return;
    }
    if (b.hasAttribute("data-move-source")) {
      const i = Number(b.dataset.moveSource),
        j = i + Number(b.dataset.dir);
      [sourceSelection[i], sourceSelection[j]] = [
        sourceSelection[j],
        sourceSelection[i],
      ];
      drawAssets();
      return;
    }
    if (b.dataset.removeSource) {
      sourceSelection = sourceSelection.filter(
        (x) => x !== b.dataset.removeSource,
      );
      drawAssets();
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
        index = Number(b.dataset.media);
        renderPlayer();
        renderTimeline();
        if (view === "result") await showCopy();
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
    $("captionPreview").hidden = true;
    modalKind = "";
  });
  $("composer").onsubmit = (e) => {
    e.preventDefault();
    action(send);
  };
  $("prompt").onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      action(send);
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
    if (!document.hidden && session) refresh();
  });
  $("openNavigation").onclick = () => {
    $("navigationPanel").showModal();
    $("openNavigation").setAttribute("aria-expanded", "true");
  };
  $("closeNavigation").onclick = () => $("navigationPanel").close();
  $("navigationPanel").addEventListener("close", () =>
    $("openNavigation").setAttribute("aria-expanded", "false"),
  );
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
    await window.QuickNav.render({
      container: "#quickNav",
      auth,
      activeKey: "videoEditingAi",
    });
    if (!auth.authenticated) {
      $("gate").textContent = "请登录后进入智能剪辑工作台";
      return;
    }
    caps = await api("/studio/capabilities");
    $("gate").hidden = true;
    $("workspace").hidden = false;
    if (/^#ss_[a-f0-9]{32}$/.test(location.hash))
      await loadSession(location.hash.slice(1));
    else render();
  }
  init().catch((e) => {
    $("gate").hidden = false;
    $("gate").textContent = "工作台暂时无法打开：" + e.message;
  });
})();
