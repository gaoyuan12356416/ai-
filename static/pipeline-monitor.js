(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const CHANNELS = { x: "X", fb: "Facebook", tt: "TikTok", youtube: "YouTube" };
  const STAGES = { prepare: "制作", publish: "发布" };
  const HOSTS = { "cpu-main": "CPU 服务器", "hk-gpu": "香港 GPU" };
  const STATES = { queued: "排队中", pending: "排队中", running: "执行中", preparing: "制作中", publishing: "发布中", ready: "已就绪", completed: "制作完成", published: "发布成功", failed: "失败", unknown: "结果待核对", backlog_paused: "历史积压暂停", available: "正常", healthy: "正常", unavailable: "采集失败", offline: "离线", stale: "数据延迟", idle: "空闲", busy: "执行中", draining: "排空中", legacy_running: "原流程执行中" };
  const PHASES = { download: "下载素材", probe: "校验素材", render: "制作渲染", upload_artifact: "保存成片", prepare: "制作", upload: "上传平台", media_uploading: "上传平台", processing: "平台处理", post_creating: "创建帖子", publish: "发布", reconcile: "核对回执", complete: "完成" };
  Object.assign(STATES, { waiting: "待执行", executing: "执行中", scheduled: "预约中", blocked: "受阻", paused: "历史暂停", closed: "已取消/策略关闭" });
  Object.assign(PHASES, { artifact_upload: "成片传输", artifact_transfer: "成片传输", platform_submit: "平台提交", publish_initialize: "平台提交", platform_upload: "平台上传", platform_processing: "平台处理", platform_confirmation_wait: "平台确认等待", queue: "排队", create_post: "创建帖子", completed: "完成" });
  const IDLE_REASONS = { no_eligible_task: "暂无到期可领取任务", capacity_limit: "已达到本机当前容量", account_busy: "相关账号正在执行或等待核对", channel_limit: "已达到渠道并发上限", global_publish_limit: "已达到全局发布上限", capability_unavailable: "待办所需能力与本机不匹配", channel_disabled: "渠道尚未启用新接单", node_offline: "节点心跳未就绪", draining: "停止接单，等待在途结束", worker_busy: "当前进程已有任务", account_ordering: "等待同账号前序任务", cooldown: "平台限流冷却中", awaiting_assignment: "等待调度" };
  Object.assign(IDLE_REASONS, { draining: "已停止新领取，无在途任务，等待退出", draining_busy: "已停止新领取，当前任务完成后退出", heartbeat_stale_with_task: "心跳已过期，任务尚未结束，请核对进程状态" });
  const e = value => String(value == null ? "" : value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const number = value => value == null || value === "" || !Number.isFinite(Number(value)) ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 1 });
  function duration(value) { if (value == null || value === "" || !Number.isFinite(Number(value))) return "—"; const seconds = Math.max(0, Number(value)); return seconds < 60 ? `${Math.round(seconds)}秒` : seconds < 3600 ? `${(seconds / 60).toFixed(1)}分` : `${(seconds / 3600).toFixed(1)}小时`; }
  function time(value) { const stamp = value ? new Date(value) : null; return stamp && Number.isFinite(stamp.getTime()) ? stamp.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "—"; }
  const badge = state => `<span class="pm-badge ${e(String(state || "").replace(/[^a-z_]/g, ""))}">${e(STATES[state] || state || "—")}</span>`;
  function detailUrl(value) { try { const url = new URL(value, location.origin); return value && url.origin === location.origin && /^\/[a-z0-9][a-z0-9_./-]*\.html$/i.test(url.pathname) ? url.pathname + url.search + url.hash : ""; } catch (_) { return ""; } }
  function notice(id, message) { $(id).textContent = message || ""; $(id).hidden = !message; }
  let auth = null, cursor = "", nextCursor = "", history = [], loading = false, queuedRefresh = false, requestGeneration = 0, capacity = null, capacityBusy = false, timer = null, enrollmentText = "", enrollmentTimer = null;
  async function api(path, options) {
    const response = await fetch(path, Object.assign({ credentials: "same-origin", cache: "no-store", headers: { "Content-Type": "application/json" } }, options || {}));
    let data; try { data = await response.json(); } catch (_) { throw new Error("服务返回异常，请稍后刷新；上次数据保留。"); }
    if (!response.ok) { const error = new Error(data.message || (response.status === 401 ? "登录已过期，请重新登录。" : "数据暂不可用，请稍后刷新。")); error.status = response.status; throw error; }
    return data;
  }
  function filters(tasks) { const values = { window: $("pmWindow").value, channel: $("pmChannel").value }; if (tasks) Object.assign(values, { stage: $("pmStage").value, status: $("pmStatus").value, host_id: $("pmHost").value, cursor, limit: "50" }); return new URLSearchParams(Object.entries(values).filter(([, v]) => v !== "")).toString(); }
  function totals(data) {
    const t = data.totals || {};
    $("pmTotals").innerHTML = [["waiting", "待执行", ""], ["executing", "执行中", ""], ["scheduled", "预约中", ""], ["abnormal", "异常", "alert"], ["prepared", "制作完成", ""], ["published", "发布成功", "accent"], ["backlog_paused", "历史积压暂停", "alert"]].map(([key, label, style]) => `<article class="pm-stat ${style}" data-stat-key="${key}"><span class="pm-stat-label">${label}</span><div class="pm-stat-value">${number(t[key])}</div></article>`).join("");
  }
  function abnormalDetails(row) {
    const labels = { failed: "失败", unknown: "待核对", held: "暂停或限制", blocked_by_unknown: "等待异常账号核对", worker_offline: "执行器失联", repeated_claim: "反复领取未推进", schedule_missing: "预约时间缺失", identity_missing: "账号身份缺失" };
    return Object.entries(row.abnormal_reasons || {}).filter(([, count]) => count > 0).map(([reason, count]) => `<small>${e(row.reason_labels?.[reason] || labels[reason] || "其他异常")} ${number(count)}</small>`).join("");
  }
  // Keep headers and cells in the same asset so cached HTML cannot swap
  // successful completions with unresolved or failed work after an update.
  const CHANNEL_COLUMNS = [
    { key: "scheduled", label: "预约中", cell: row => number(row.scheduled) },
    { key: "waiting", label: "待执行", cell: row => number(row.waiting) },
    { key: "executing", label: "执行中", cell: row => number(row.executing) },
    { key: "completed", label: "已完成", cell: row => `<strong>${number(row.completed)}</strong>` },
    { key: "abnormal", label: "范围内异常", title: "仅统计所选时间范围内仍未解决的异常，原因明细采用相同范围", cell: row => `${number(row.abnormal)}${abnormalDetails(row)}` },
    { key: "throughput_per_hour", label: "吞吐", title: "最近 1 小时成功完成数（条/小时）", cell: row => number(row.throughput_per_hour) },
    { key: "avg_duration_seconds", label: "平均耗时", cell: row => duration(row.avg_duration_seconds) },
    { key: "oldest_pending_seconds", label: "最老等待", title: "已到期且待执行任务的最长等待时间", cell: row => `${duration(row.oldest_pending_seconds)}${row.backlog_paused ? `<small>历史暂停 ${number(row.backlog_paused)}</small>` : ""}` },
  ];
  function renderSummary(data) {
    totals(data);
    $("pmScope").textContent = data.scope === "all" ? "全站任务" : "我的任务";
    $("pmTaskScope").textContent = data.scope === "all" ? "显示获准渠道的全站任务；保留原账号、素材与发布回执。" : "仅显示当前登录用户在获准渠道内的任务。";
    const allowed = Array.isArray(data.allowed_channels) ? data.allowed_channels : [];
    const selected = $("pmChannel").value;
    $("pmChannel").innerHTML = '<option value="">全部可见渠道</option>' + allowed.filter(c => CHANNELS[c]).map(c => `<option value="${c}">${CHANNELS[c]}</option>`).join("");
    $("pmChannel").value = allowed.includes(selected) ? selected : "";
    $("pmUpdated").textContent = `更新于 ${time(data.collected_at || data.generated_at)}（北京时间）`;
    $("pmLiveDot").classList.toggle("stale", !!data.stale);
    const warnings = (data.warnings || []).map(v => typeof v === "string" ? v : v.message || v.code || "").filter(Boolean);
    if (data.queue_state_version !== 1) warnings.unshift("任务分类数据尚未更新，四类计数暂显示为 —。");
    if (data.stale) warnings.unshift("采集数据有延迟，以下保留最近一次可用快照。");
    notice("pmWarnings", [...new Set(warnings)].join("；"));
    $("pmWindowLabel").textContent = data.window?.start && data.window?.end ? `${time(data.window.start)} — ${time(data.window.end)}` : $("pmWindow").selectedOptions[0].textContent;
    const channelRows = $("pmChannelRows");
    channelRows.closest("table").querySelector("thead tr").innerHTML = '<th>渠道 / 阶段</th>' + CHANNEL_COLUMNS.map(column => `<th data-column="${column.key}"${column.title ? ` title="${e(column.title)}"` : ""}>${column.label}</th>`).join("");
    channelRows.innerHTML = (data.channels || []).map(row => `<tr><td><span class="pm-channel"><b class="pm-channel-icon">${e({ x: "X", fb: "FB", tt: "TT", youtube: "YT" }[row.channel] || "·")}</b><span><strong>${e(CHANNELS[row.channel] || row.channel)}</strong><small>${e(STAGES[row.stage] || row.stage)}</small></span></span></td>${CHANNEL_COLUMNS.map(column => `<td data-column="${column.key}">${column.cell(row)}</td>`).join("")}</tr>`).join("") || '<tr><td colspan="9" class="pm-empty">当前范围暂无可见的渠道任务数据</td></tr>';
    $("pmSources").innerHTML = (data.sources || []).map(row => `<div class="pm-source"><strong>${e(CHANNELS[row.channel] || row.channel)}</strong><span>${e(row.family || "")}</span>${badge(row.state)}<span title="采集时间">${e(time(row.collected_at))}</span>${row.error_code ? `<span>${e(row.error_code)}</span>` : ""}</div>`).join("") || '<p class="pm-muted">尚无采集状态，等待采集器上报。</p>';
    $("pmEfficiencyRows").innerHTML = (data.channels || []).map(row => { const phases = (data.efficiency || []).filter(p => p.channel === row.channel && p.stage === row.stage); return `<tr><td>${e(CHANNELS[row.channel])} · ${e(STAGES[row.stage])}</td><td>${row.success_rate == null ? "未记录" : number(row.success_rate * 100) + "%"}</td><td>${number(row.throughput_per_hour)}</td>${["download", "render", "artifact_transfer", "platform_submit", "platform_upload", "platform_processing", "platform_confirmation_wait", "queue"].map(phase => { const timing = phases.find(p => p.phase === phase); return `<td>${timing?.samples ? `${duration(timing.p50_seconds)} / ${duration(timing.p95_seconds)}<small>${number(timing.samples)} 个样本</small>` : timing?.applicable === false ? "不适用" : "未记录"}</td>`; }).join("")}</tr>`; }).join("") || '<tr><td colspan="11" class="pm-empty">暂无已完成的计时样本</td></tr>';
  }
  function meter(label, value, text) { const valid = value != null && Number.isFinite(Number(value)); return `<div><div class="pm-resource-head"><span>${e(label)}</span><b>${e(text == null ? (valid ? `${number(value)}%` : "—") : text)}</b></div><div class="pm-meter"><span style="width:${valid ? Math.max(0, Math.min(100, Number(value))) : 0}%"></span></div></div>`; }
  function memory(used, total) { return { percent: used != null && total > 0 ? used / total * 100 : null, text: used != null && total > 0 ? `${(used / 1073741824).toFixed(1)} / ${(total / 1073741824).toFixed(1)} GB` : "—" }; }
  function renderWorkerStage(stage) {
    const busy = stage.busy_workers, alive = stage.alive_workers;
    const idle = stage.idle_workers;
    // Older monitor DTOs already classify fresh workers exclusively. Infer the
    // omitted draining count only when the complete count set is consistent.
    const inferred = [busy, idle, alive].every(v => Number.isInteger(v) && v >= 0) && alive >= busy + idle ? alive - busy - idle : null;
    const draining = stage.draining_workers == null || stage.draining_workers === inferred ? inferred : null;
    const notes = [];
    if (draining > 0) notes.push(`${number(draining)} 个排空进程已停止新领取，无在途任务，等待退出。`);
    if (stage.draining_busy_workers > 0) notes.push(`${number(stage.draining_busy_workers)} 个忙碌进程已停止新领取，当前任务完成后退出。`);
    if (Number.isFinite(busy) && Number.isFinite(stage.max_workers) && busy > stage.max_workers) notes.push(`自然排空：当前 ${number(busy)} 个任务继续完成，目标并发 ${number(stage.max_workers)}，暂不补充任务。`);
    else if (stage.max_workers === 0 && !notes.length) notes.push("已停止新领取。");
    return `<div class="pm-worker-stage" data-worker-stage="${e(stage.stage)}"><div><small>执行阶段</small><b>${e(STAGES[stage.stage] || stage.stage)}</b></div><div><small>当前目标 / 上限 · ${stage.scaling_mode === "manual" ? "手动" : "自动"}</small><b>${number(stage.max_workers)} / ${number(stage.ceiling)}</b></div><div><small>忙碌 / 空闲 / 排空 / 存活</small><b>${number(busy)} / ${number(idle)} / ${number(draining)} / ${number(alive)}</b></div><div><small>今日完成</small><b>${number(stage.completed_today)}</b></div><div><small>最近 1 小时</small><b>${number(stage.throughput_per_hour)} 条</b></div>${notes.length ? `<div class="pm-worker-draining">${e(notes.join(" "))}</div>` : ""}</div>`;
  }
  function processReason(process) {
    if (process.status === "offline") return IDLE_REASONS.heartbeat_stale_with_task;
    if (process.status === "draining") return IDLE_REASONS.draining;
    if (process.status === "busy" && process.draining) return IDLE_REASONS.draining_busy;
    return IDLE_REASONS[process.idle_reason] || process.idle_reason || (process.status === "busy" ? "正在处理任务" : "未上报等待原因");
  }
  function renderWorkers(data) {
    const selectedHost = $("pmHost").value;
    (data.items || []).forEach(host => { HOSTS[host.host_id] = host.label || HOSTS[host.host_id] || host.host_id; });
    $("pmHost").innerHTML = '<option value="">全部服务器</option>' + (data.items || []).map(host => `<option value="${e(host.host_id)}">${e(HOSTS[host.host_id])}</option>`).join("");
    $("pmHost").value = (data.items || []).some(h => h.host_id === selectedHost) ? selectedHost : "";
    $("pmWorkers").innerHTML = (data.items || []).map(host => { const ram = memory(host.memory_used_bytes, host.memory_total_bytes), vram = memory(host.gpu_memory_used_bytes, host.gpu_memory_total_bytes); return `<article class="pm-worker" data-worker-host="${e(host.host_id)}"><div class="pm-worker-head"><div class="pm-worker-title"><span class="pm-server-icon">${(host.gpu_count > 0 || host.gpu_memory_total_bytes > 0) ? "GPU" : "CPU"}</span><h2>${e(host.label || HOSTS[host.host_id] || host.host_id)}</h2></div>${badge(host.stale ? "stale" : "healthy")}</div><div class="pm-worker-resources">${meter("CPU 使用率", host.cpu_percent)}${meter("内存", ram.percent, ram.text)}${(host.gpu_count > 0 || host.gpu_memory_total_bytes > 0) ? meter("GPU 运算使用率", host.gpu_percent) + meter("显存", vram.percent, vram.text) + meter("视频编码器使用率", host.encoder_percent) + '<div class="pm-hint">GPU 运算与视频编码器分别统计；判断制作负载需同时查看这两项和 CPU。</div>' : ""}</div>${(host.stages || []).map(renderWorkerStage).join("")}<div class="pm-worker-foot">最近心跳 ${e(time(host.last_seen_at))} · ${host.stale ? "心跳延迟，进程数可能不是当前值" : "统计来自执行进程心跳"}</div></article>`; }).join("") || '<div class="pm-panel pm-empty">尚未收到服务器心跳；不会将缺失数据视为零进程。</div>';
    const currentProcesses = (data.processes || []).filter(process => process.status !== "offline" || process.current_task_id != null || process.current_source_key);
    $("pmProcessRows").innerHTML = currentProcesses.map(process => `<tr><td><strong>${e(HOSTS[process.host_id] || process.host_id)}</strong><small>${e(process.worker_id)}${process.pid == null ? "" : ` · PID ${e(process.pid)}`}</small></td><td>${e(STAGES[process.stage] || process.stage)}<small>${badge(process.status)}${process.status === "busy" && process.draining ? ' <span class="pm-badge draining">停止新领取</span>' : ""}</small></td><td>${e(process.current_source_key || process.current_task_id || "—")}${process.channel ? `<small>${e(CHANNELS[process.channel] || process.channel)}</small>` : ""}</td><td>${e(PHASES[process.phase] || process.phase || "—")}</td><td>${duration(process.elapsed_seconds)}</td><td>${e(time(process.last_seen_at))}</td><td>${e(processReason(process))}</td></tr>`).join("") || `<tr><td colspan="7" class="pm-empty">${e(data.visibility_message || "暂无有新鲜心跳或未结束任务的进程")}</td></tr>`;
    if (auth?.user?.is_admin && data.capacity) renderCapacity(data.capacity);
  }
  function renderCapacity(value, force) {
    if (!force && (capacityBusy || $("pmCapacityForms").contains(document.activeElement))) return;
    capacity = value;
    $("pmCapacitySection").hidden = false;
    $("pmCapacityVersion").textContent = `配置版本 ${number(value.version)}`;
    $("pmCapacityForms").innerHTML = `<div class="pm-capacity-grid">${(value.hosts || []).map((row, i) => `<form class="pm-capacity-row" data-host="${e(row.host_id)}" data-stage="${e(row.stage)}"><label for="pmCap${i}">${e(HOSTS[row.host_id] || row.host_id)} · ${e(STAGES[row.stage] || row.stage)}<small class="pm-muted"> ${row.scaling_mode === "manual" ? "手动" : "自动"} · 当前目标 ${number(row.max_workers)}，${row.scaling_mode === "manual" ? "保存后按此并发接单" : "并发上限"}</small></label><input id="pmCap${i}" name="max_workers" type="number" min="0" max="64" step="1" value="${e(row.ceiling)}" required aria-label="${e(HOSTS[row.host_id] || row.host_id)}${e(STAGES[row.stage])}并发上限"><button class="pm-button" type="submit">保存</button><button class="pm-button" type="submit" data-drain="true">停止接单</button></form>`).join("")}</div><form id="pmGlobalCapacity" class="pm-capacity-platforms"><label>发布总并发<input name="global_publish_limit" type="number" min="1" max="64" step="1" value="${e(value.global_publish_limit)}" required></label>${Object.keys(CHANNELS).map(c => `<label>${CHANNELS[c]} 发布上限<input name="channel_${c}" type="number" min="1" max="64" step="1" value="${e(value.channel_limits?.[c])}" required></label>`).join("")}<button class="pm-button primary" type="submit">保存发布限制</button></form>`;
  }
  function renderTasks(data) {
    nextCursor = data.next_cursor || "";
    $("pmPrev").disabled = !history.length; $("pmNext").disabled = !nextCursor;
    $("pmTaskCount").textContent = `共 ${number(data.total)} 条 · 每页最多 50 条`;
    $("pmTaskRows").innerHTML = (data.items || []).map(row => { const url = detailUrl(row.detail_url), title = row.source_id || row.id || row.source_key || "—"; return `<tr><td>${url ? `<a class="pm-id" href="${e(url)}">${e(title)}</a>` : `<strong class="pm-id">${e(title)}</strong>`}<small>${e(CHANNELS[row.channel] || row.channel)} · ${e(row.source_family || "")}</small></td><td>${e(row.account_label || "—")}<small>${e(row.owner_name || "归属未确认")}</small></td><td>${e(STAGES[row.stage] || row.stage)}<small>${badge(row.effective_state || row.status)}<span class="pm-muted"> · 原状态 ${e(row.status)}</span></small></td><td>${e(HOSTS[row.host_id] || row.host_id || "尚未分配")}<small>${e(row.worker_id || "")}</small></td><td>${e(time(row.created_at))}<small>开始 ${e(time(row.started_at))}</small></td><td>${duration(row.wait_seconds)} / ${duration(row.duration_seconds)}</td><td>${e(row.effective_reason || row.error_message || (row.status === "unknown" ? "等待平台结果核对，不自动重发" : row.status === "backlog_paused" ? "历史积压暂停，保留原任务" : "—"))}${(row.effective_code || row.error_code) ? `<small>${e(row.effective_code || row.error_code)}</small>` : ""}</td></tr>`; }).join("") || '<tr><td colspan="7" class="pm-empty">当前筛选没有可见任务</td></tr>';
  }
  async function refresh() {
    if (!auth?.authenticated) return;
    if (loading) { queuedRefresh = true; return; }
    loading = true; $("pmRefresh").disabled = true; const generation = requestGeneration;
    const requests = [["summary", filters(false), renderSummary], ["workers", filters(false), renderWorkers], ["tasks", filters(true), renderTasks]];
    try {
      const results = await Promise.allSettled(requests.map(([view, query, render]) => api(`/api/pipeline-monitor/${view}?${query}`).then(data => {
        if (generation === requestGeneration) render(data);
        return data;
      })));
      if (generation !== requestGeneration) { queuedRefresh = true; return; }
      const errors = [];
      results.forEach(result => { if (result.status === "rejected") errors.push(result.reason?.message || "部分数据读取失败"); });
      notice("pmError", errors.length ? [...new Set(errors)].join("；") : "");
      if (errors.length) $("pmLiveDot").classList.add("stale");
    } finally {
      loading = false; $("pmRefresh").disabled = false;
      if (queuedRefresh) { queuedRefresh = false; refresh(); }
    }
  }
  function changeFilters() { cursor = ""; history = []; nextCursor = ""; requestGeneration++; refresh(); }
  async function saveCapacity(event) {
    const form = event.target.closest("form"); if (!form || !capacity || capacityBusy) return;
    event.preventDefault(); if (!form.reportValidity()) return;
    const fields = new FormData(form), payload = { expected_version: capacity.version };
    if (form.dataset.host) Object.assign(payload, { host_id: form.dataset.host, stage: form.dataset.stage, max_workers: event.submitter?.dataset.drain ? 0 : Number(fields.get("max_workers")) });
    else Object.assign(payload, { global_publish_limit: Number(fields.get("global_publish_limit")), channel_limits: Object.fromEntries(Object.keys(CHANNELS).map(c => [c, Number(fields.get(`channel_${c}`))])) });
    capacityBusy = true; form.querySelectorAll("button,input").forEach(input => input.disabled = true); notice("pmCapacityError", "");
    try { const result = await api("/api/pipeline-monitor/capacity", { method: "PUT", body: JSON.stringify(payload) }); if (result.capacity) renderCapacity(result.capacity, true); else capacity = null; document.activeElement?.blur(); await refresh(); }
    catch (error) { notice("pmCapacityError", error.message); }
    finally { capacityBusy = false; form.querySelectorAll("button,input").forEach(input => input.disabled = false); if (!capacity) refresh(); }
  }
  function clearEnrollment() { enrollmentText = ""; clearTimeout(enrollmentTimer); $("pmEnrollmentConfig").textContent = ""; $("pmEnrollmentExpiry").textContent = ""; $("pmEnrollment").hidden = true; }
  async function enroll(event) {
    event.preventDefault(); const form = event.target; if (!form.reportValidity()) return;
    const fields = new FormData(form), button = form.querySelector("button"); button.disabled = true; clearEnrollment(); notice("pmEnrollError", "");
    try {
      const result = await api("/api/pipeline-monitor/nodes/enroll", { method: "POST", body: JSON.stringify({ label: fields.get("label") }) });
      enrollmentText = JSON.stringify(result, null, 2); $("pmEnrollmentConfig").textContent = enrollmentText; $("pmEnrollmentExpiry").textContent = `有效期至 ${time(result.expires_at)}（北京时间），仅可使用一次。`; $("pmEnrollment").hidden = false;
      const expiry = Date.parse(result.expires_at); enrollmentTimer = setTimeout(clearEnrollment, Math.max(1, Math.min(900000, Number.isFinite(expiry) ? expiry - Date.now() : 900000)));
    } catch (error) { notice("pmEnrollError", error.message); } finally { button.disabled = false; }
  }
  async function boot() {
    $("pmShowNav").onclick = () => $("pmSidebar").classList.add("open"); $("pmHideNav").onclick = () => $("pmSidebar").classList.remove("open");
    $("pmRefresh").onclick = refresh; $("pmCapacityForms").onsubmit = saveCapacity;
    $("pmEnrollOpen").onclick = () => { $("pmEnrollSection").hidden = false; };
    $("pmEnrollClose").onclick = () => { clearEnrollment(); $("pmEnrollSection").hidden = true; };
    $("pmEnrollForm").onsubmit = enroll;
    $("pmCopyEnrollment").onclick = async () => { try { if (enrollmentText) await navigator.clipboard.writeText(enrollmentText); } catch (_) { notice("pmEnrollError", "无法自动复制，请手动选中接入配置复制。"); } };
    ["pmWindow", "pmChannel", "pmStage", "pmStatus", "pmHost"].forEach(id => $(id).onchange = changeFilters);
    $("pmPrev").onclick = () => { cursor = history.pop() || ""; requestGeneration++; refresh(); };
    $("pmNext").onclick = () => { if (!nextCursor) return; history.push(cursor); cursor = nextCursor; requestGeneration++; refresh(); };
    try {
      auth = await api("/api/ui/topbar"); UiTopbar.render({ auth, userCard: "#userCard", authButton: "#authButton" });
      $("authButton").onclick = () => UiTopbar.handleAuthAction({ auth, api });
      Promise.resolve(QuickNav.render({ container: "#quickNav", auth, activeKey: "pipelineMonitor" })).catch(() => notice("pmError", "导航加载失败，请刷新重试。"));
      if (!auth.authenticated) { $("pmGate").textContent = "请先登录 AI 自动后台查看制作与发布监测。"; return; }
      $("pmEnrollOpen").hidden = !auth.user?.is_admin;
      $("pmGate").hidden = true; $("pmContent").hidden = false; totals({}); await refresh();
      timer = setInterval(() => { if (!document.hidden && !capacityBusy) refresh(); }, 15000);
      document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
      window.addEventListener("pagehide", () => { clearInterval(timer); clearEnrollment(); }, { once: true });
    } catch (error) { $("pmGate").textContent = "监测页面暂不可用，请刷新重试。"; notice("pmError", error.message); }
  }
  // Export pure render helpers for the browser contract tests without credentials.
  window.PipelineMonitor = { escape: e, number, duration, time, detailUrl, renderSummary, renderWorkers, renderTasks };
  boot();
})();
