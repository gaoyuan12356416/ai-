(function () {
  "use strict";
  const API = "/api/fb-post-ad-delete";
  const PHASES = ["creative", "ad", "video"];
  const PHASE_NAMES = { creative: "Creative", ad: "Ad", video: "Video" };
  const JOB_STATES = {
    previewing: ["正在预览", "info"], ready: ["预览就绪", "info"], failed: ["任务失败", "error"],
    running: ["正在执行", "info"], completed: ["处理完成", "success"], partial: ["部分完成", "warning"],
    interrupted: ["执行已中断", "warning"],
  };
  const OBJECT_STATES = {
    pending: ["待执行", "neutral"], in_progress: ["执行中", "info"], deleted: ["删除成功", "success"],
    already_deleted: ["已确认删除", "success"], failed: ["失败", "error"], blocked: ["已阻止", "warning"],
    unknown: ["结果待核实", "purple"],
  };
  const EXECUTABLE_STATES = new Set(["ready", "completed", "partial", "interrupted"]);
  const $ = id => document.getElementById(id);
  const all = selector => Array.from(document.querySelectorAll(selector));
  const list = value => Array.isArray(value) ? value : [];
  const num = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const str = value => value == null ? "" : String(value);
  const esc = value => str(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const state = {
    auth: null, permitted: false, products: [], productsLoaded: false, selectedProducts: new Set(),
    job: null, activeJobId: null, jobs: [], formGeneration: 0, boundGeneration: -1,
    query: { page: 1, kind: "", status: "" }, displayedQuery: { page: 1, kind: "", status: "" }, detailSeq: 0, detailBusy: false,
    previewBusy: false, executing: false, reconciling: false, rechecking: false, historyBusy: false,
    poll: null, pollErrors: 0, confirmation: null, blockedSeq: 0, pendingAction: null, pendingRecheck: null, storageKey: "",
  };
  const htmlCache = new Map();
  let toastTimer;

  function html(id, value) {
    if (htmlCache.get(id) === value) return;
    $(id).innerHTML = value;
    htmlCache.set(id, value);
  }
  function show(id, visible) { $(id).classList.toggle("hidden", !visible); }
  function message(id, text) { $(id).textContent = text || ""; show(id, !!text); }
  function toast(text) {
    $("toast").textContent = text;
    $("toast").classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $("toast").classList.remove("show"), 4000);
  }
  function badge(value, map) {
    const item = map[value] || [str(value) || "未知", "neutral"];
    return '<span class="tag ' + item[1] + '">' + esc(item[0]) + "</span>";
  }
  function time(value) {
    if (!value) return "—";
    let dateValue = value;
    if (typeof value === "number") dateValue = value < 100000000000 ? value * 1000 : value;
    if (typeof dateValue === "string" && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(dateValue) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(dateValue)) {
      dateValue = dateValue.replace(" ", "T") + "Z";
    }
    const date = new Date(dateValue);
    return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(date);
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    let response;
    try {
      response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, signal: controller.signal });
      const raw = await response.text();
      let data;
      try { data = raw ? JSON.parse(raw) : {}; }
      catch (_) {
        const error = new Error("服务器响应无法解析，请刷新任务核实当前状态。");
        error.status = response.status;
        error.uncertain = response.ok || response.status >= 500;
        throw error;
      }
      if (!response.ok) {
        const error = new Error(typeof data.message === "string" ? data.message : (typeof data.error === "string" ? data.error : "请求失败（HTTP " + response.status + "）"));
        error.status = response.status;
        error.code = data.error;
        error.uncertain = response.status >= 500 || response.status === 408;
        throw error;
      }
      return data;
    } catch (error) {
      if (!response || error.status == null) {
        // DOMException.message can be read-only; wrap it without losing the
        // uncertain outcome flag required to retain the same execution ID.
        const networkError = new Error(error.name === "AbortError" ? "请求超时，请核实任务状态。" : "网络连接失败，请稍后重试。");
        networkError.uncertain = true;
        throw networkError;
      }
      throw error;
    } finally { clearTimeout(timer); }
  }
  function post(path, payload) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  }
  function ids() { return [...new Set($("idsInput").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean))]; }
  function phases() { return PHASES.filter(kind => all("[data-phase]").some(input => input.value === kind && input.checked)); }
  function phaseResult(kind, job = state.job) { return (job && job.phase_results && job.phase_results[kind]) || {}; }
  function eligible(kind, job = state.job) { const result = phaseResult(kind, job); return num(result.pending) + num(result.failed); }
  function selectedEligible(job = state.job, selected = phases()) { return selected.reduce((count, kind) => count + eligible(kind, job), 0); }
  function legacy(job = state.job) { return !!job && (job.read_only === true || !job.preview_id); }
  function recheckRunning(job = state.job) { return !!job && !legacy(job) && !!job.recheck && job.recheck.status === "running"; }
  function jobBusy(job = state.job) { return !!job && (["previewing", "running"].includes(job.status) || recheckRunning(job)); }
  function dirty() { return !!state.job && state.formGeneration !== state.boundGeneration; }
  function actionBusy() { return state.previewBusy || state.executing || state.reconciling || state.rechecking || state.detailBusy; }
  function canExecute() {
    return state.permitted && !!state.job && !legacy() && !dirty() && !actionBusy() && !jobBusy() && !state.pendingAction && !state.pendingRecheck &&
      state.activeJobId === state.job.job_id && EXECUTABLE_STATES.has(state.job.status) && phases().length > 0 && selectedEligible() > 0;
  }
  function canRecheck() {
    return state.permitted && !!state.job && !legacy() && !dirty() && !actionBusy() && !jobBusy() &&
      !state.pendingAction && !state.pendingRecheck && state.activeJobId === state.job.job_id &&
      EXECUTABLE_STATES.has(state.job.status) && num(state.job.summary && state.job.summary.blocked) > 0;
  }
  function productById(id, job = state.job) {
    return state.products.find(item => str(item.id) === str(id)) ||
      list(job && job.products).find(item => str(item.id) === str(id)) || { id: str(id), name: "产品 " + str(id), kind: "" };
  }
  function productLabel(product) { return str(product.name || "产品") + " · " + str(product.id) + (product.kind ? " · " + product.kind : ""); }
  function parentLabel(product) { return "所属主产品：" + str(product.parent_name || product.parent_id || "—") + (product.parent_name && product.parent_id ? "（" + product.parent_id + "）" : ""); }

  function renderProducts() {
    const query = $("productSearch").value.trim().toLocaleLowerCase();
    const matching = state.products.filter(product => [product.name, product.id, product.kind, product.parent_name, product.parent_id].map(str).join(" ").toLocaleLowerCase().includes(query));
    html("productOptions", matching.map(product => {
      const selected = state.selectedProducts.has(str(product.id));
      return '<label class="product-option' + (selected ? " selected" : "") + '"><input type="checkbox" data-product-id="' + esc(product.id) + '"' + (selected ? " checked" : "") + ' /><span class="product-option-label"><span class="product-option-name">' + esc(product.name) + ' <span class="tag ' + (product.kind === "W2A" ? "purple" : "info") + '">' + esc(product.kind) + '</span></span><span class="product-option-meta">ID ' + esc(product.id) + " · " + esc(parentLabel(product)) + "</span></span></label>";
    }).join("") || '<div class="empty">' + (state.productsLoaded ? (state.products.length ? "没有匹配的产品" : "暂无有权限的短剧投放产品") : "产品列表尚未加载") + "</div>");
    $("productMatchCount").textContent = "共 " + matching.length + " 个产品";
    $("productCount").textContent = "已选 " + state.selectedProducts.size + " / 20";
    html("productSummary", (state.selectedProducts.size ? "已选择 " + state.selectedProducts.size + " 个投放产品" : "请选择投放产品") + '<span class="chevron" aria-hidden="true"></span>');
    html("selectedProducts", [...state.selectedProducts].map(id => {
      const product = productById(id);
      const unavailable = !state.products.some(item => str(item.id) === id);
      return '<span class="product-chip' + (unavailable ? " unavailable" : "") + '"><span>' + esc(productLabel(product)) + (unavailable ? "（当前不可选）" : "") + '</span><button type="button" data-remove-product="' + esc(id) + '" aria-label="移除 ' + esc(productLabel(product)) + '">×</button></span>';
    }).join("") || '<p class="selection-empty">尚未选择产品</p>');
  }
  function updateForm() {
    const values = ids();
    $("idCount").textContent = values.length + " / 50 个";
    $("idCount").style.color = values.length > 50 ? "var(--bad)" : "";
    const isSeries = $("inputType").value === "series_code";
    $("idsInput").placeholder = isSeries ? "每行输入一个资源 ID" : "每行输入一个剧 ID";
    $("inputHelp").textContent = isSeries ? "资源 ID 将展开为该资源下所有语言版本的剧 ID。" : "精确匹配对应剧集及语言版本，每行一个剧 ID。";
    $("scopeHint").textContent = dirty() ? "范围已变更，需重新预览。" : state.job ? "任务范围以本次预览的固定清单为准。" : "先预览对象，再确认执行。";
    updateControls();
  }
  function invalidateScope() {
    state.formGeneration += 1;
    closeConfirmation(true);
    message("formError", "");
    updateForm();
  }
  function updateControls() {
    const busy = actionBusy();
    const selected = phases();
    $("previewBtn").disabled = !state.permitted || !state.productsLoaded || busy || jobBusy() || !!state.pendingAction || !!state.pendingRecheck;
    $("previewBtn").textContent = state.previewBusy ? "正在提交预览…" : "预览匹配对象 →";
    $("executeBtn").disabled = !canExecute();
    $("executeBtn").textContent = state.executing ? "正在提交…" : state.job && ["partial", "interrupted", "completed"].includes(state.job.status) ? "继续或重试所选阶段" : "执行所选阶段";
    $("executeBtn").title = dirty() ? "输入范围已变更，请重新预览" : !selected.length ? "请至少选择一个阶段" : !selectedEligible() ? "所选阶段没有待执行或明确失败项" : "";
    $("phaseOrder").textContent = selected.length ? selected.map(kind => PHASE_NAMES[kind]).join(" → ") : "请选择至少一个删除阶段";
    all("[data-phase]").forEach(input => { input.disabled = state.executing || state.reconciling || state.rechecking || jobBusy() || !!state.pendingRecheck; });
    show("staleNotice", dirty() && !legacy());
    show("recheckBtn", !!state.job && !legacy() && num(state.job.summary && state.job.summary.blocked) > 0);
    $("recheckBtn").disabled = !canRecheck();
    $("recheckBtn").textContent = state.rechecking || recheckRunning() ? "正在重新核验…" : "重新核验阻止项";
    $("recheckBtn").title = dirty() ? "输入范围已变更，请重新预览" : "重新核验原任务中的阻止项，保留固定对象清单；不会执行删除";
    show("reconcileBtn", !legacy() && num(state.job && state.job.summary && state.job.summary.unknown) > 0);
    $("reconcileBtn").disabled = !state.permitted || legacy() || dirty() || busy || jobBusy() || !!state.pendingAction || !!state.pendingRecheck;
    $("reconcileBtn").textContent = state.reconciling ? "正在核实…" : "核实待确认结果";
    $("refreshJobBtn").disabled = !state.job || busy;
    $("refreshJobsBtn").disabled = state.historyBusy;
    $("resolveRequestBtn").disabled = !state.permitted || busy || !!state.pendingRecheck || recheckRunning();
    $("resolveRequestBtn").textContent = state.executing ? "正在核对…" : "核对执行请求";
    show("requestNotice", !!state.pendingAction);
    $("requestNoticeText").textContent = state.pendingAction ? "任务 " + state.pendingAction.job_id + "。将使用同一请求编号核对已确认的执行操作。" : "";
    show("recheckRequestNotice", !!state.pendingRecheck);
    $("recheckRequestText").textContent = state.pendingRecheck ? "任务 " + state.pendingRecheck.job_id + "。可先刷新任务查看进度，或使用同一请求编号核对。本操作仅重新核验，不会执行删除。" : "";
    $("resolveRecheckRequestBtn").disabled = !state.permitted || busy || !!state.pendingAction || jobBusy();
    $("resolveRecheckRequestBtn").textContent = state.rechecking ? "正在核对…" : "核对核验请求";
    const resultPages = Math.max(1, Math.ceil(num(state.job && state.job.total) / 50));
    $("prevPageBtn").disabled = !state.job || state.detailBusy || state.query.page <= 1;
    $("nextPageBtn").disabled = !state.job || state.detailBusy || state.query.page >= resultPages;
    $("kindFilter").disabled = !state.job || state.detailBusy;
    $("statusFilter").disabled = !state.job || state.detailBusy;
    all("[data-job]").forEach(button => { button.disabled = state.executing || state.reconciling || state.rechecking || state.previewBusy; });
    if (state.confirmation) {
      $("confirmExecuteBtn").disabled = state.executing || !state.confirmation.blockedReady || !canExecute();
      $("confirmExecuteBtn").textContent = state.executing ? "正在提交…" : "确认删除 " + selectedEligible(state.confirmation.job, state.confirmation.phases) + " 个对象";
      $("cancelConfirmBtn").disabled = state.executing;
      $("closeConfirmBtn").disabled = state.executing;
    }
  }
  function validateInput() {
    const inputIds = ids();
    if (!inputIds.length) throw new Error("请至少输入一个" + ($("inputType").value === "series_code" ? "资源 ID。" : "剧 ID。"));
    if (inputIds.length > 50) throw new Error("一次最多输入 50 个 ID，请分批处理。");
    if (inputIds.some(id => /[\s,，]/.test(id))) throw new Error("请每行输入一个 ID，不要使用空格或逗号分隔。");
    if (!state.selectedProducts.size) throw new Error("请至少选择一个短剧投放产品。");
    if (state.selectedProducts.size > 20) throw new Error("一次最多选择 20 个投放产品。");
    const allowed = new Set(state.products.map(product => str(product.id)));
    if ([...state.selectedProducts].some(id => !allowed.has(id))) throw new Error("已选范围中存在当前不可用的产品，请移除后重新预览。");
    return { input_type: $("inputType").value, ids: inputIds, product_ids: [...state.selectedProducts] };
  }

  function dramaRows(dramas) {
    return list(dramas).map(drama => '<tr><td><strong>' + esc(drama.product_name || productById(drama.product_id).name) + '</strong><span class="cell-sub">ID ' + esc(drama.product_id) + '</span></td><td class="mono">' + esc(drama.content_id) + '</td><td class="mono">' + esc(drama.series_code || "—") + '</td><td>' + esc(drama.language || "—") + '<span class="cell-sub">' + esc(drama.name || "—") + "</span></td></tr>").join("") || '<tr><td colspan="4" class="empty">没有解析到剧集</td></tr>';
  }
  function blockerRows(blockers) {
    return list(blockers).map(item => "<li>" + esc(item.message || item.code || "归属尚未确认") + '<span class="cell-sub">' + esc([item.product_id ? "产品 " + item.product_id : "", item.input_id ? "输入 ID " + item.input_id : ""].filter(Boolean).join(" · ")) + "</span></li>").join("");
  }
  function safeReason(item) {
    if (typeof item.reason === "string" && item.reason) return item.reason;
    const result = item.result || {};
    if (typeof result.message === "string") return result.message;
    if (typeof result.error === "string") return result.error;
    if (result.error && typeof result.error.message === "string") return result.error.message;
    return "";
  }
  function diagnosticDetails(item) {
    const result = item.result && typeof item.result === "object" ? item.result : {};
    const detail = result.detail && typeof result.detail === "object" ? result.detail : {};
    const account = result.account_diagnostic || detail.account_diagnostic || {};
    // Render only supported, server-sanitized scalar fields, never raw responses.
    const scalar = value => ["string", "number", "boolean"].includes(typeof value) ? str(value) : "";
    const code = scalar(detail.code == null ? result.code : detail.code);
    const subcode = scalar(detail.error_subcode == null ? result.error_subcode : detail.error_subcode);
    const accountStatus = scalar(account.account_status == null ? account.status : account.account_status);
    const rows = [
      ["Meta 提示", scalar(detail.error_user_title)],
      ["处理建议", scalar(detail.error_user_msg)],
      ["错误码 / 子码", [code, subcode].filter(Boolean).join(" / ")],
      ["请求追踪 ID", scalar(detail.fbtrace_id || result.fbtrace_id)],
      ["账户状态", [accountStatus, scalar(account.account_state)].filter(Boolean).join(" · ")],
      ["账户诊断", scalar(account.message)],
      ["凭证用户", scalar(result.credential_user_id || detail.credential_user_id)],
    ].filter(row => row[1] !== "");
    if (!rows.length) return "";
    return '<details class="object-diagnostic"><summary>查看诊断详情</summary><dl>' +
      rows.map(([label, value]) => "<dt>" + esc(label) + "</dt><dd>" + esc(value) + "</dd>").join("") + "</dl></details>";
  }
  function renderObjects(job) {
    const objects = list(job.objects);
    const total = num(job.total);
    $("detailCount").textContent = "共 " + total + " 个对象" + (state.query.kind || state.query.status ? "（当前筛选）" : " · 相同对象已去重");
    html("detailBody", objects.map(item => {
      const products = list(item.product_ids).map(id => {
        const product = productById(id, job);
        return "<div>" + esc(product.name) + '<span class="cell-sub">ID ' + esc(id) + (product.kind ? " · " + esc(product.kind) : "") + "</span></div>";
      }).join("");
      const content = '<span class="mono">' + esc(list(item.content_ids).join("、") || "—") + '</span><span class="cell-sub">资源 ' + esc(list(item.series_codes).join("、") || "—") + '</span><span class="cell-sub">语言 ' + esc(list(item.languages).join("、") || "—") + "</span>";
      return '<tr data-object-key="' + esc(item.key || item.object_id) + '"><td><strong>' + esc(PHASE_NAMES[item.kind] || item.kind) + '</strong><span class="cell-sub mono">' + esc(item.object_id) + '</span></td><td><div class="cell-lines">' + (products || "—") + '</div></td><td class="mono">' + esc(list(item.account_ids).join("、") || "—") + "</td><td>" + content + "</td><td>" + badge(item.status, OBJECT_STATES) + '<span class="cell-reason">' + esc(safeReason(item)) + "</span>" + diagnosticDetails(item) + "</td></tr>";
    }).join("") || '<tr><td colspan="5" class="empty">' + (job.status === "previewing" ? "正在解析剧集并核查 Meta 对象，请稍候…" : "当前范围下没有匹配对象") + "</td></tr>");
    const pages = Math.max(1, Math.ceil(total / 50));
    $("pageText").textContent = "第 " + state.query.page + " / " + pages + " 页";
  }
  function renderJob(job) {
    state.job = job;
    show("resultPanel", true);
    show("objectsPanel", !legacy(job));
    show("executionControls", !legacy(job));
    show("legacyNotice", legacy(job));
    show("legacySummary", legacy(job));
    show("stats", !legacy(job));
    show("dramaDetails", !legacy(job));
    html("jobStatus", (recheckRunning(job) ? '<span class="tag info">正在重新核验</span>' : badge(job.status, JOB_STATES)) + (legacy(job) ? ' <span class="tag neutral">历史只读</span>' : ""));
    $("jobStatus").className = "";
    $("jobMeta").textContent = "任务 " + str(job.job_id) + " · 更新于 " + time(job.updated_at || job.updated_at_utc || job.created_at || job.created_at_utc) + "（北京时间）";
    const summary = job.summary || {};
    const stats = [
      ["总对象", num(summary.total), ""], ["待执行", num(summary.pending), ""],
      ["已删除", num(summary.deleted) + num(summary.already_deleted), "success"],
      ["失败", num(summary.failed), "error"], ["已阻止", num(summary.blocked), "warning"],
      ["待核实", num(summary.unknown), "warning"],
    ];
    html("stats", stats.map(([label, count, style]) => '<div class="stat ' + style + '"><strong>' + count.toLocaleString("zh-CN") + "</strong><span>" + label + "</span></div>").join(""));
    const dramas = list(job.dramas);
    $("dramaCount").textContent = dramas.length;
    html("dramaBody", dramaRows(dramas));
    const blockers = list(job.blockers);
    $("blockerCount").textContent = blockers.length;
    show("blockerDetails", !legacy(job) && blockers.length > 0);
    html("blockerList", blockerRows(blockers));
    PHASES.forEach(kind => {
      const result = phaseResult(kind, job);
      $(kind + "Count").textContent = num(result.total).toLocaleString("zh-CN");
      const pieces = [
        ["待执行", num(result.pending)], ["执行中", num(result.in_progress)],
        ["已删除", num(result.deleted) + num(result.already_deleted)], ["失败", num(result.failed)],
        ["阻止", num(result.blocked)], ["待核实", num(result.unknown)],
      ].filter(item => item[1] > 0);
      $(kind + "Summary").textContent = pieces.map(item => item[0] + " " + item[1]).join(" · ") || (job.status === "previewing" ? "正在核查…" : "无匹配对象");
    });
    let notice = "";
    let tone = "info";
    if (job.status === "previewing") notice = (typeof job.preview_step === "string" && job.preview_step ? job.preview_step + "。" : "正在解析剧集、匹配广告并核查 Meta 关联关系。") + "预览完成后可确认执行。";
    else if (job.status === "running") notice = "任务正在执行" + (num(summary.in_progress) ? "，当前有 " + num(summary.in_progress) + " 个对象处理中" : "") + "。页面会自动更新结果。";
    else if (job.status === "interrupted") { notice = "任务已中断。请先核实待确认结果，再手动继续或重试所选阶段。"; tone = "warning"; }
    else if (job.status === "failed") { notice = typeof job.message === "string" ? job.message : typeof job.error === "string" ? job.error : job.error && typeof job.error.message === "string" ? job.error.message : "任务未能完成，请查看范围核查提示并重新预览。"; tone = "error"; }
    else if (num(summary.unknown)) { notice = "有 " + num(summary.unknown) + " 个对象结果待核实；请先核实，系统不会直接重试这些对象。"; tone = "warning"; }
    else if (job.status !== "previewing" && !legacy(job) && !num(summary.total)) notice = "当前产品及剧集范围未匹配到 Meta 广告资产，可核对输入后重新预览。";
    message("jobNotice", notice);
    $("jobNotice").className = "notice " + tone + (notice ? "" : " hidden");
    renderRecheck(job);
    if (legacy(job)) {
      const logs = list(job.legacy_logs).slice(-20).map(entry => '<li><span class="mono">' + esc(time(entry.ts || entry.created_at)) + "</span> · " + esc(typeof entry === "string" ? entry : entry.message || "—") + "</li>").join("");
      html("legacySummary", "<strong>历史范围：</strong>" + esc(list(job.series_ids || job.ids).join("、") || "未记录") + "<br>Ad " + num(summary.ads) + " · Post " + num(summary.post_objects) + " · Campaign " + num(summary.campaigns) + "<br>历史阶段：" + esc(job.phase || "—") + "。历史任务不支持继续执行。" + (logs ? '<details class="scope-details"><summary>最近 20 条历史日志</summary><ul class="blocker-list">' + logs + "</ul></details>" : ""));
    } else renderObjects(job);
    all("[data-job]").forEach(button => button.classList.toggle("active", button.dataset.job === job.job_id));
    updateForm();
  }

  function renderRecheck(job) {
    const check = !legacy(job) && job.recheck;
    const visible = !!check && ["running", "completed", "interrupted"].includes(check.status);
    show("recheckNotice", visible);
    if (!visible) return;
    const checked = num(check.checked);
    const total = num(check.total);
    const running = check.status === "running";
    const interrupted = check.status === "interrupted";
    $("recheckNotice").className = "notice recheck-notice " + (interrupted || check.error && (check.error.code || check.error.message) ? "warning" : "info");
    $("recheckTitle").textContent = running ? "正在重新核验" : interrupted ? "重新核验已中断" : "重新核验已完成";
    $("recheckStep").textContent = typeof check.step === "string" ? check.step : "";
    $("recheckSummary").textContent = "已核验 " + checked + " / " + total + " 个对象" +
      (check.updated_at ? " · " + time(check.updated_at) + "（北京时间）" : "") + "。" +
      (running ? "保留原任务的固定对象清单，页面会自动更新。" : interrupted ? "已保存核验进度，可手动重新核验阻止项。" : "请查看最新对象状态，可删除的阻止项已转为待执行。") +
      "本操作不会执行删除，删除仍需另行确认。";
    const progress = $("recheckProgress");
    progress.max = total || 1;
    if (total) progress.value = Math.min(checked, total);
    else progress.removeAttribute("value");
    show("recheckProgress", running);
    const error = check.error || {};
    $("recheckError").textContent = [typeof error.message === "string" ? error.message : "", typeof error.code === "string" ? "（" + error.code + "）" : ""].filter(Boolean).join(" ");
  }

  function stopPolling() { if (state.poll) clearTimeout(state.poll); state.poll = null; }
  function schedulePoll() {
    stopPolling();
    if (!jobBusy() || state.activeJobId !== state.job.job_id) return;
    state.poll = setTimeout(async () => {
      state.poll = null;
      if (document.hidden || state.executing || state.rechecking || state.detailBusy) { schedulePoll(); return; }
      try { await loadJob(state.activeJobId, { polling: true }); state.pollErrors = 0; }
      catch (error) {
        state.pollErrors += 1;
        if (state.pollErrors === 1) message("actionError", "任务自动更新失败：" + error.message + " 已保留当前结果，稍后自动重试。");
        schedulePoll();
      }
    }, 2500);
  }
  function hydrateForm(job) {
    $("inputType").value = job.input_type === "series_code" ? "series_code" : "content_id";
    $("idsInput").value = list(job.ids).map(str).join("\n");
    state.selectedProducts = new Set(list(job.products).map(product => str(product.id)));
    state.formGeneration += 1;
    state.boundGeneration = state.formGeneration;
    $("productSearch").value = "";
    renderProducts();
  }
  function detailPath(jobId, query = state.query) {
    const params = new URLSearchParams({ page: str(query.page || 1), page_size: "50" });
    if (query.kind) params.set("kind", query.kind);
    if (query.status) params.set("status", query.status);
    return API + "/jobs/" + encodeURIComponent(jobId) + "?" + params.toString();
  }
  async function loadJob(jobId, options = {}) {
    if (!jobId) return;
    const seq = ++state.detailSeq;
    const generation = state.formGeneration;
    const previousStatus = state.job && state.job.job_id === jobId ? state.job.status : null;
    const previousRecheckStatus = state.job && state.job.job_id === jobId && state.job.recheck ? state.job.recheck.status : null;
    stopPolling();
    if (options.hydrate) {
      closeConfirmation(true);
      state.activeJobId = jobId;
      state.query = { page: 1, kind: "", status: "" };
      $("kindFilter").value = "";
      $("statusFilter").value = "";
    }
    state.detailBusy = true;
    updateControls();
    try {
      const job = await api(detailPath(jobId));
      if (seq !== state.detailSeq || state.activeJobId !== jobId) return;
      if (options.hydrate && generation === state.formGeneration && !legacy(job)) hydrateForm(job);
      else if (options.hydrate) state.boundGeneration = -1;
      state.query.page = num(job.page) || state.query.page;
      state.displayedQuery = { ...state.query };
      const pending = state.pendingRecheck;
      if (pending && pending.job_id === job.job_id && pending.payload.preview_id === job.preview_id &&
          job.recheck && job.recheck.operation_id && job.recheck.operation_id !== pending.previous_operation_id) persistRecheck(null);
      renderJob(job);
      if (!options.polling) message("actionError", "");
      if ((previousStatus && previousStatus !== job.status) || previousRecheckStatus !== (job.recheck && job.recheck.status || null)) loadJobs().catch(() => {});
    } catch (error) {
      if (seq === state.detailSeq) {
        state.activeJobId = state.job ? state.job.job_id : null;
        state.query = { ...state.displayedQuery };
        $("kindFilter").value = state.query.kind;
        $("statusFilter").value = state.query.status;
        if (!options.polling) message("actionError", error.message);
      }
      throw error;
    } finally {
      if (seq === state.detailSeq) { state.detailBusy = false; updateControls(); schedulePoll(); }
    }
  }
  async function preview() {
    if ($("previewBtn").disabled) return;
    let payload;
    try { payload = validateInput(); } catch (error) { message("formError", error.message); return; }
    const generation = state.formGeneration;
    state.previewBusy = true;
    closeConfirmation(true);
    $("productPicker").open = false;
    message("formError", "");
    updateControls();
    try {
      const job = await post(API + "/preview", payload);
      if (!job.job_id || !job.preview_id) throw new Error("预览响应缺少任务信息，请刷新任务记录核实。");
      ++state.detailSeq;
      state.detailBusy = false;
      state.activeJobId = job.job_id;
      state.boundGeneration = generation;
      state.query = { page: 1, kind: "", status: "" };
      state.displayedQuery = { ...state.query };
      $("kindFilter").value = "";
      $("statusFilter").value = "";
      renderJob(job);
      message("actionError", "");
      schedulePoll();
      loadJobs().catch(() => {});
      toast(job.status === "previewing" ? "预览任务已建立，正在核查对象。" : "预览已完成，请核对范围。");
    } catch (error) { message("formError", error.message); }
    finally { state.previewBusy = false; updateControls(); }
  }
  async function loadProducts() {
    try {
      const data = await api(API + "/products");
      state.products = list(data.items).map(product => ({ ...product, id: str(product.id) }));
      state.productsLoaded = true;
      message("productsError", "");
      renderProducts();
    } catch (error) {
      state.productsLoaded = false;
      message("productsError", "产品加载失败：" + error.message + " 请刷新页面重试。");
      html("productOptions", '<div class="empty">产品列表加载失败</div>');
    }
    updateControls();
  }
  async function loadJobs() {
    if (state.historyBusy) return;
    state.historyBusy = true;
    updateControls();
    try {
      const data = await api(API + "/jobs");
      state.jobs = list(data.items);
      html("jobsBox", state.jobs.map(job => {
        const summary = job.summary || {};
        const input = list(job.ids || job.series_ids).join("、") || str(job.job_id);
        const products = list(job.products).map(product => str(product.name || product.id)).join("、");
        const isLegacy = legacy(job);
        const count = isLegacy ? "Ad " + num(summary.ads) + " / Post " + num(summary.post_objects) : num(summary.total) + " 个对象";
        return '<button type="button" class="job-card' + (state.job && state.job.job_id === job.job_id ? " active" : "") + '" data-job="' + esc(job.job_id) + '"><div><strong title="' + esc(input) + '">' + (isLegacy ? "历史任务 · " : job.input_type === "series_code" ? "资源 ID · " : "剧 ID · ") + esc(input) + '</strong><small>' + esc(products || "产品范围见任务详情") + " · " + esc(time(job.created_at || job.created_at_utc)) + '</small></div><div class="job-right">' + (isLegacy ? '<span class="tag neutral">历史只读</span>' : recheckRunning(job) ? '<span class="tag info">正在重新核验</span>' : badge(job.status, JOB_STATES)) + '<span class="job-quantity">' + count + "</span></div></button>";
      }).join("") || '<div class="empty">暂无任务。预览后会在这里保留记录。</div>');
    } catch (error) {
      if (!state.jobs.length) html("jobsBox", '<div class="empty">任务记录加载失败：' + esc(error.message) + "</div>");
      else toast("刷新任务记录失败：" + error.message);
      throw error;
    } finally { state.historyBusy = false; updateControls(); }
  }

  function closeConfirmation(force = false) {
    if (state.executing && !force) return;
    if ($("confirmDialog").open) $("confirmDialog").close();
    state.confirmation = null;
    ++state.blockedSeq;
  }
  async function openConfirmation() {
    if (!canExecute()) return;
    const jobId = state.job.job_id;
    try { await loadJob(jobId); } catch (_) { return; }
    if (!canExecute()) { toast("任务状态已更新，请重新核对可执行对象。"); return; }
    const selected = phases();
    const job = JSON.parse(JSON.stringify(state.job));
    const blockedKinds = selected.filter(kind => num(phaseResult(kind, job).blocked) > 0);
    state.confirmation = {
      job, job_id: job.job_id, preview_id: job.preview_id, phases: selected,
      generation: state.formGeneration, blockedPage: 1, blockedReady: blockedKinds.length === 0,
    };
    html("confirmProducts", list(job.products).map(product => '<span class="product-chip">' + esc(productLabel(product)) + "<small>" + esc(parentLabel(product)) + "</small></span>").join(""));
    $("confirmDramaCount").textContent = "共 " + list(job.dramas).length + " 条";
    html("confirmDramaBody", dramaRows(job.dramas));
    $("confirmOrder").textContent = selected.map(kind => PHASE_NAMES[kind]).join(" → ");
    html("confirmPhaseBody", selected.map(kind => {
      const result = phaseResult(kind, job);
      return "<tr><td>" + PHASE_NAMES[kind] + "</td><td>" + eligible(kind, job) + "</td><td>" + (num(result.deleted) + num(result.already_deleted)) + "</td><td>" + num(result.blocked) + "</td><td>" + num(result.unknown) + "</td></tr>";
    }).join(""));
    const blockers = list(job.blockers);
    show("confirmScopeBlockers", blockers.length > 0);
    html("confirmScopeBlockerList", blockerRows(blockers));
    show("confirmBlockedSection", blockedKinds.length > 0);
    html("confirmBlockedKind", blockedKinds.map(kind => '<option value="' + kind + '">' + PHASE_NAMES[kind] + " · " + num(phaseResult(kind, job).blocked) + " 个</option>").join(""));
    message("confirmError", "");
    $("confirmDialog").showModal();
    $("cancelConfirmBtn").focus();
    updateControls();
    if (blockedKinds.length) await loadConfirmationBlocked();
  }
  async function loadConfirmationBlocked() {
    const confirmation = state.confirmation;
    if (!confirmation) return;
    const seq = ++state.blockedSeq;
    const kind = $("confirmBlockedKind").value;
    const page = confirmation.blockedPage;
    confirmation.blockedReady = false;
    $("confirmBlockedKind").disabled = true;
    $("confirmBlockedPrev").disabled = true;
    $("confirmBlockedNext").disabled = true;
    html("confirmBlockedList", '<div class="empty">正在加载被阻止对象…</div>');
    updateControls();
    try {
      const data = await api(detailPath(confirmation.job_id, { page, kind, status: "blocked" }));
      if (state.confirmation !== confirmation || seq !== state.blockedSeq) return;
      html("confirmBlockedList", list(data.objects).map(item => '<div class="confirm-blocked-row"><strong class="mono">' + esc(item.object_id) + "</strong> · " + esc(PHASE_NAMES[item.kind] || item.kind) + "<br>" + esc(safeReason(item) || "归属核查未通过") + "</div>").join("") || '<div class="empty">当前没有被阻止对象</div>');
      const pages = Math.max(1, Math.ceil(num(data.total) / 50));
      $("confirmBlockedPage").textContent = "第 " + page + " / " + pages + " 页 · 共 " + num(data.total) + " 个";
      $("confirmBlockedPrev").disabled = page <= 1;
      $("confirmBlockedNext").disabled = page >= pages;
      confirmation.blockedReady = true;
      message("confirmError", "");
    } catch (error) {
      if (state.confirmation !== confirmation || seq !== state.blockedSeq) return;
      html("confirmBlockedList", '<div class="empty">阻止明细加载失败。<button type="button" class="text-btn" data-retry-blocked>重新加载</button></div>');
      message("confirmError", error.message);
    } finally {
      if (state.confirmation === confirmation && seq === state.blockedSeq) {
        $("confirmBlockedKind").disabled = false;
        updateControls();
      }
    }
  }
  function persistPending(action) {
    state.pendingAction = action;
    try {
      if (action) sessionStorage.setItem(state.storageKey, JSON.stringify(action));
      else sessionStorage.removeItem(state.storageKey);
    } catch (_) { /* The server also fences every object and request ID. */ }
    updateControls();
  }
  function persistRecheck(action) {
    state.pendingRecheck = action;
    try {
      if (action) sessionStorage.setItem(state.storageKey + ":recheck", JSON.stringify(action));
      else sessionStorage.removeItem(state.storageKey + ":recheck");
    } catch (_) { /* The server also deduplicates this read-only request. */ }
    updateControls();
  }
  function restorePending() {
    try {
      const value = JSON.parse(sessionStorage.getItem(state.storageKey) || "null");
      if (value && typeof value.job_id === "string" && value.payload && typeof value.payload.preview_id === "string" &&
          /^[a-f0-9-]{36}$/i.test(value.payload.request_id) && list(value.payload.phases).length &&
          value.payload.phases.every(kind => PHASES.includes(kind))) state.pendingAction = value;
    } catch (_) { /* An unreadable local hint never authorizes execution. */ }
    try {
      const value = JSON.parse(sessionStorage.getItem(state.storageKey + ":recheck") || "null");
      if (value && typeof value.job_id === "string" && value.payload && typeof value.payload.preview_id === "string" &&
          /^[a-f0-9-]{36}$/i.test(value.payload.request_id)) state.pendingRecheck = value;
    } catch (_) { /* A missing hint does not change server-side task state. */ }
    updateControls();
  }
  async function sendExecution(action) {
    if (state.executing) return;
    state.executing = true;
    stopPolling();
    persistPending(action);
    message("actionError", "");
    message("confirmError", "");
    let accepted;
    try {
      accepted = await post(API + "/jobs/" + encodeURIComponent(action.job_id) + "/execute", action.payload);
      if (str(accepted.job_id) !== action.job_id || !accepted.run_id) {
        const error = new Error("执行响应不完整，请核对本次执行请求。");
        error.uncertain = true;
        throw error;
      }
      persistPending(null);
      closeConfirmation(true);
      toast(accepted.duplicate ? "已核对到同一次执行请求，未重复创建。" : "删除任务已开始。");
    } catch (error) {
      if (!error.uncertain) {
        persistPending(null);
        message(state.confirmation ? "confirmError" : "actionError", error.message);
      } else {
        closeConfirmation(true);
        message("actionError", error.message + " 本次请求编号已保留，请使用“核对执行请求”。");
      }
    } finally { state.executing = false; updateControls(); }
    if (accepted && !state.pendingAction) {
      if (state.activeJobId !== action.job_id) {
        try { await loadJob(action.job_id, { hydrate: true }); } catch (_) {}
      } else {
        // Show a conservative running state if readback is temporarily unavailable.
        if (state.job) renderJob({ ...state.job, status: "running" });
        try { await loadJob(action.job_id); } catch (_) { schedulePoll(); }
      }
      loadJobs().catch(() => {});
    } else schedulePoll();
  }
  async function confirmExecution() {
    const confirmation = state.confirmation;
    if (!confirmation || !canExecute() || !confirmation.blockedReady || state.executing) return;
    if (confirmation.job_id !== state.job.job_id || confirmation.preview_id !== state.job.preview_id ||
        confirmation.generation !== state.formGeneration || confirmation.phases.join(",") !== phases().join(",")) {
      closeConfirmation(true);
      toast("执行范围已变更，请重新预览并确认。");
      return;
    }
    if (!window.crypto || typeof window.crypto.randomUUID !== "function") {
      message("confirmError", "当前浏览器无法生成安全请求编号，请使用新版浏览器通过 HTTPS 访问后台。");
      return;
    }
    const action = {
      job_id: confirmation.job_id,
      payload: { preview_id: confirmation.preview_id, phases: [...confirmation.phases], request_id: window.crypto.randomUUID() },
    };
    await sendExecution(action);
  }
  async function reconcile() {
    if ($("reconcileBtn").disabled || !state.job) return;
    const jobId = state.job.job_id;
    const previewId = state.job.preview_id;
    state.reconciling = true;
    message("actionError", "");
    updateControls();
    try {
      const result = await post(API + "/jobs/" + encodeURIComponent(jobId) + "/reconcile", { preview_id: previewId });
      await loadJob(jobId);
      const remaining = num(state.job && state.job.summary && state.job.summary.unknown);
      toast("已核实 " + num(result.checked) + " 个对象。" + (remaining ? "仍有 " + remaining + " 个结果待核实，可再次核实。" : "请查看最新对象状态。"));
      loadJobs().catch(() => {});
    } catch (error) { message("actionError", "结果核实失败：" + error.message); }
    finally { state.reconciling = false; updateControls(); }
  }

  async function sendRecheck(action) {
    if (state.rechecking || state.executing || state.reconciling || !state.permitted) return;
    state.rechecking = true;
    stopPolling();
    closeConfirmation(true);
    persistRecheck(action);
    message("actionError", "");
    let accepted;
    let failure = "";
    let uncertainFailure = false;
    try {
      accepted = await post(API + "/jobs/" + encodeURIComponent(action.job_id) + "/recheck", action.payload);
      const operationId = accepted.operation_id || accepted.recheck && accepted.recheck.operation_id;
      if (str(accepted.job_id) !== action.job_id || !operationId || accepted.read_only !== true) {
        const error = new Error("重新核验响应不完整，请刷新任务核实进度。");
        error.uncertain = true;
        throw error;
      }
      persistRecheck(null);
      if (state.job && state.job.job_id === action.job_id) {
        // Recheck has its own state: keep the execution status and frozen IDs.
        renderJob({ ...state.job, recheck: { ...(accepted.recheck || {}), operation_id: operationId,
          status: "running", checked: 0, total: num(state.job.summary && state.job.summary.blocked),
          step: "正在读取重新核验进度" } });
      }
      toast(accepted.duplicate ? "已核对到同一次重新核验请求。" : "已开始重新核验阻止项，不会执行删除。");
    } catch (error) {
      failure = error.message;
      uncertainFailure = !!error.uncertain;
      if (!error.uncertain) persistRecheck(null);
      else failure += " 本次请求编号已保留，将先刷新任务核实进度。";
    } finally { state.rechecking = false; updateControls(); }
    try { await loadJob(action.job_id, { hydrate: state.activeJobId !== action.job_id }); }
    catch (_) { schedulePoll(); }
    if (failure && (!uncertainFailure || state.pendingRecheck)) message("actionError", failure);
    loadJobs().catch(() => {});
  }
  async function recheck() {
    if (!canRecheck()) return;
    const jobId = state.job.job_id;
    try { await loadJob(jobId); } catch (_) { return; }
    if (!canRecheck()) { toast("任务状态已更新，请重新核对阻止项。"); return; }
    if (!window.crypto || typeof window.crypto.randomUUID !== "function") {
      message("actionError", "当前浏览器无法生成安全请求编号，请使用新版浏览器通过 HTTPS 访问后台。");
      return;
    }
    await sendRecheck({
      job_id: jobId,
      previous_operation_id: state.job.recheck && state.job.recheck.operation_id || "",
      payload: { preview_id: state.job.preview_id, request_id: window.crypto.randomUUID() },
    });
  }

  function bindEvents() {
    const authAction = () => window.UiTopbar.handleAuthAction({ auth: state.auth || {}, api }).catch(error => toast(error.message));
    $("authBtn").addEventListener("click", authAction);
    $("gateLoginBtn").addEventListener("click", authAction);
    $("retryInitBtn").addEventListener("click", () => location.reload());
    $("idsInput").addEventListener("input", invalidateScope);
    $("inputType").addEventListener("change", invalidateScope);
    $("productSearch").addEventListener("input", renderProducts);
    $("productPicker").addEventListener("toggle", () => { if ($("productPicker").open) $("productSearch").focus(); });
    $("productOptions").addEventListener("change", event => {
      const input = event.target.closest("[data-product-id]");
      if (!input) return;
      if (input.checked && state.selectedProducts.size >= 20) {
        input.checked = false;
        toast("一次最多选择 20 个投放产品。");
        return;
      }
      if (input.checked) state.selectedProducts.add(input.dataset.productId);
      else state.selectedProducts.delete(input.dataset.productId);
      renderProducts();
      const next = all("[data-product-id]").find(element => element.dataset.productId === input.dataset.productId);
      if (next) next.focus();
      invalidateScope();
    });
    $("selectedProducts").addEventListener("click", event => {
      const button = event.target.closest("[data-remove-product]");
      if (!button) return;
      state.selectedProducts.delete(button.dataset.removeProduct);
      renderProducts();
      invalidateScope();
    });
    $("clearProductsBtn").addEventListener("click", () => { state.selectedProducts.clear(); renderProducts(); invalidateScope(); });
    $("previewBtn").addEventListener("click", preview);
    $("phaseChoices").addEventListener("change", () => { closeConfirmation(true); updateControls(); });
    $("executeBtn").addEventListener("click", () => openConfirmation().catch(error => message("actionError", error.message)));
    $("recheckBtn").addEventListener("click", recheck);
    $("reconcileBtn").addEventListener("click", reconcile);
    $("refreshJobBtn").addEventListener("click", () => loadJob(state.activeJobId).catch(() => {}));
    $("refreshJobsBtn").addEventListener("click", () => loadJobs().catch(() => {}));
    $("jobsBox").addEventListener("click", event => {
      const button = event.target.closest("[data-job]");
      if (button && !button.disabled) loadJob(button.dataset.job, { hydrate: true }).catch(error => toast(error.message));
    });
    ["kindFilter", "statusFilter"].forEach(id => $(id).addEventListener("change", () => {
      state.query = { page: 1, kind: $("kindFilter").value, status: $("statusFilter").value };
      loadJob(state.activeJobId).catch(error => toast(error.message));
    }));
    $("prevPageBtn").addEventListener("click", () => { if ($("prevPageBtn").disabled) return; state.query.page -= 1; loadJob(state.activeJobId).catch(error => toast(error.message)); });
    $("nextPageBtn").addEventListener("click", () => { if ($("nextPageBtn").disabled) return; state.query.page += 1; loadJob(state.activeJobId).catch(error => toast(error.message)); });
    ["closeConfirmBtn", "cancelConfirmBtn"].forEach(id => $(id).addEventListener("click", () => closeConfirmation()));
    $("confirmDialog").addEventListener("cancel", event => { event.preventDefault(); closeConfirmation(); });
    $("confirmExecuteBtn").addEventListener("click", confirmExecution);
    $("resolveRequestBtn").addEventListener("click", () => { if (state.pendingAction && !$("resolveRequestBtn").disabled) sendExecution(state.pendingAction); });
    $("resolveRecheckRequestBtn").addEventListener("click", () => { if (state.pendingRecheck && !$("resolveRecheckRequestBtn").disabled) sendRecheck(state.pendingRecheck); });
    $("confirmBlockedKind").addEventListener("change", () => { if (state.confirmation) { state.confirmation.blockedPage = 1; loadConfirmationBlocked(); } });
    $("confirmBlockedPrev").addEventListener("click", () => { if (state.confirmation && !$("confirmBlockedPrev").disabled) { state.confirmation.blockedPage -= 1; loadConfirmationBlocked(); } });
    $("confirmBlockedNext").addEventListener("click", () => { if (state.confirmation && !$("confirmBlockedNext").disabled) { state.confirmation.blockedPage += 1; loadConfirmationBlocked(); } });
    $("confirmBlockedList").addEventListener("click", event => { if (event.target.closest("[data-retry-blocked]")) loadConfirmationBlocked(); });
    $("navToggle").addEventListener("click", () => {
      const open = $("sidebar").classList.toggle("is-open");
      $("navToggle").setAttribute("aria-expanded", str(open));
    });
    document.addEventListener("click", event => {
      if (!$("productPicker").contains(event.target)) $("productPicker").open = false;
      if (!$("sidebar").contains(event.target) && !$("navToggle").contains(event.target)) {
        $("sidebar").classList.remove("is-open"); $("navToggle").setAttribute("aria-expanded", "false");
      }
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      $("productPicker").open = false;
      $("sidebar").classList.remove("is-open");
      $("navToggle").setAttribute("aria-expanded", "false");
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) schedulePoll(); });
    window.addEventListener("pagehide", stopPolling);
  }
  async function init() {
    bindEvents();
    try {
      state.auth = await api("/api/ui/topbar");
      window.UiTopbar.render({ auth: state.auth, userCard: "#userCard", authButton: "#authBtn", refreshButton: "#refreshPageBtn" });
      await window.QuickNav.render({
        container: "#quickNav", activeKey: "fbPostAdDelete", auth: state.auth,
        onNavigate: (item, event) => {
          const link = event.target.closest("a.nav-item");
          const href = (link && link.getAttribute("href")) || item.href || (item.view ? "/#" + item.view : "/");
          const target = new URL(href.startsWith("#") ? "/" + href : href, location.origin + "/");
          if (!["http:", "https:"].includes(target.protocol)) { toast("导航地址不可用。"); return; }
          location.href = target.href;
        },
      });
      show("loadingGate", false);
      if (!state.auth.authenticated) { show("loginGate", true); return; }
      const user = state.auth.user || {};
      state.permitted = !!(user.is_admin || (user.permissions || {}).fb_ad_asset_delete);
      if (!state.permitted) { show("permissionGate", true); return; }
      state.storageKey = "metaDramaAssetDeletePending:" + str(user.tenant_key) + ":" + str(user.user_id || user.id || user.email || user.name);
      $("historyScope").textContent = (user.is_admin ? "管理员可查看全部任务" : "仅显示当前账号的任务") + " · 北京时间";
      show("pageRoot", true);
      restorePending();
      updateForm();
      await Promise.allSettled([loadProducts(), loadJobs()]);
      if (state.pendingAction) await loadJob(state.pendingAction.job_id, { hydrate: true }).catch(error => message("actionError", error.message));
      else if (state.pendingRecheck) await loadJob(state.pendingRecheck.job_id, { hydrate: true }).catch(error => message("actionError", error.message));
    } catch (error) {
      show("loadingGate", false);
      show("loadErrorGate", true);
      $("loadErrorText").textContent = error.message;
    }
  }
  document.addEventListener("DOMContentLoaded", init);
})();
