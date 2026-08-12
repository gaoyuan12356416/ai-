(function () {
  "use strict";

  const ui = window.XAutoPublish;
  const state = {
    page: 1,
    pageSize: 20,
    total: 0,
    runs: [],
    templates: [],
    pollTimer: 0,
    detailRequest: 0,
  };

  const STATUS_LABELS = {
    queued: "等待执行",
    scheduled: "等待执行",
    running: "执行中",
    pending: "等待执行",
    selecting: "筛选中",
    filtering: "筛选中",
    no_candidate: "没有合适素材",
    reserved: "素材已锁定",
    preparing: "视频准备中",
    retry_wait: "等待重试",
    ready: "等待发布",
    publishing: "发布中",
    reconciling: "结果核对中",
    completed: "已完成",
    partial_failed: "部分失败",
    published: "已发布",
    failed: "失败",
    needs_review: "结果待确认",
    unknown: "结果待确认",
    canceled: "已取消",
    skipped: "已跳过",
  };

  function runId(item) {
    return ui.positiveId(item && (item.id || item.run_id));
  }

  function statusLabel(value) {
    const normalized = String(value || "").toLowerCase();
    return STATUS_LABELS[normalized] || value || "未知";
  }

  function triggerLabel(value) {
    const normalized = String(value || "").toLowerCase();
    if (["manual", "run_now"].includes(normalized)) return "手动执行";
    if (["auto", "scheduled", "timer", "automatic"].includes(normalized)) return "自动定时";
    return value || "—";
  }

  function countValue(item, names, fallback) {
    for (const name of names) {
      const value = Number(item && item[name]);
      if (Number.isFinite(value)) return value;
    }
    return fallback;
  }

  function setEmpty(message) {
    const body = ui.byId("runRows");
    ui.clear(body);
    const row = ui.element("tr");
    row.appendChild(ui.element("td", { className: "empty", text: message, attributes: { colspan: "8" } }));
    body.appendChild(row);
  }

  function renderTemplateOptions() {
    const select = ui.byId("filterTemplateId");
    const selected = select.value || new URLSearchParams(location.search).get("template_id") || "";
    ui.clear(select);
    select.appendChild(ui.element("option", { text: "全部模板", attributes: { value: "" } }));
    state.templates.forEach(item => {
      const id = ui.positiveId(item.id || item.template_id);
      if (!id) return;
      const option = ui.element("option", { text: item.name || `模板 ${id}`, attributes: { value: id } });
      if (id === selected) option.selected = true;
      select.appendChild(option);
    });
  }

  function renderStats(payload) {
    const summary = ui.objectValue(payload && (payload.summary || payload.counts));
    const activeStatuses = new Set(["queued", "scheduled", "running", "filtering", "preparing", "publishing", "reconciling"]);
    const completeStatuses = new Set(["completed", "published"]);
    const attentionStatuses = new Set(["partial_failed", "failed", "needs_review"]);
    ui.setText(ui.byId("totalCount"), state.total, "0");
    ui.setText(ui.byId("activeCount"), ui.numberValue(summary.active, state.runs.filter(item => activeStatuses.has(String(item.status || "").toLowerCase())).length), "0");
    ui.setText(ui.byId("completedCount"), ui.numberValue(summary.completed, state.runs.filter(item => completeStatuses.has(String(item.status || "").toLowerCase())).length), "0");
    ui.setText(ui.byId("attentionCount"), ui.numberValue(summary.attention, state.runs.filter(item => attentionStatuses.has(String(item.status || "").toLowerCase())).length), "0");
  }

  function renderRuns() {
    const body = ui.byId("runRows");
    ui.clear(body);
    if (!state.runs.length) {
      setEmpty("没有符合条件的运行记录。");
      return;
    }
    state.runs.forEach(item => {
      const id = runId(item);
      const status = String(item.status || "").toLowerCase();
      const row = ui.element("tr");
      ui.appendTextCell(row, ui.formatTime(item.triggered_at || item.created_at || item.scheduled_at || item.scheduled_at_utc));

      const templateCell = ui.element("td");
      const primary = ui.element("div", { className: "primary-cell" });
      primary.appendChild(ui.element("strong", { text: item.template_name || `模板 ${item.template_id || "—"}` }));
      primary.appendChild(ui.element("span", { className: "secondary mono", text: `Run ${id || "—"} · v${item.template_version || item.version || "—"}` }));
      templateCell.appendChild(primary);
      row.appendChild(templateCell);

      ui.appendTextCell(row, triggerLabel(item.trigger_type));

      const totalTasks = countValue(item, ["task_count", "account_count", "total_tasks"], 0);
      const completedTasks = countValue(item, ["completed_task_count", "completed_accounts", "completed_tasks"], 0);
      const failedTasks = countValue(item, ["attention_task_count", "failed_task_count", "failed_accounts", "failed_tasks"], 0);
      const tasksCell = ui.element("td");
      tasksCell.appendChild(ui.element("strong", { text: `${completedTasks} / ${totalTasks || "—"} 完成` }));
      if (failedTasks) tasksCell.appendChild(ui.element("div", { className: "secondary", text: `${failedTasks} 个失败或待确认` }));
      row.appendChild(tasksCell);

      const statusCell = ui.element("td");
      statusCell.appendChild(ui.statusBadge(statusLabel(status), ui.statusKind(status)));
      row.appendChild(statusCell);

      const summary = item.result_summary || item.summary || item.message || item.error_message
        || (totalTasks
          ? `${completedTasks} 个完成${failedTasks ? `，${failedTasks} 个失败或待确认` : ""}`
          : "—");
      ui.appendTextCell(row, summary);
      ui.appendTextCell(row, ui.formatTime(item.completed_at || item.finished_at || item.finished_at_utc));

      const actionCell = ui.element("td");
      actionCell.appendChild(ui.element("button", {
        className: "button small",
        text: "查看详情",
        type: "button",
        dataset: { runId: id },
      }));
      row.appendChild(actionCell);
      body.appendChild(row);
    });
  }

  function renderPager() {
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    ui.setText(ui.byId("pageInfo"), `第 ${state.page} / ${pages} 页，共 ${state.total} 条`, "");
    ui.byId("prevPage").disabled = state.page <= 1;
    ui.byId("nextPage").disabled = state.page >= pages;
  }

  function queryValues() {
    return {
      template_id: ui.byId("filterTemplateId").value,
      trigger_type: ui.byId("filterTriggerType").value,
      status: ui.byId("filterStatus").value,
      from: ui.byId("filterFrom").value,
      to: ui.byId("filterTo").value,
      limit: state.pageSize,
      offset: (state.page - 1) * state.pageSize,
    };
  }

  async function loadRuns(options) {
    const quiet = !!(options && options.quiet);
    if (!quiet) setEmpty("正在加载运行记录…");
    try {
      const payload = await ui.api(`${ui.API_BASE}/runs?${ui.queryString(queryValues())}`);
      state.runs = ui.readItems(payload, ["runs", "items"]);
      state.total = Math.max(0, ui.numberValue(payload.total || (payload.pagination && payload.pagination.total), state.runs.length));
      renderRuns();
      renderStats(payload);
      renderPager();
      syncPolling();
    } catch (error) {
      if (!quiet) {
        state.runs = [];
        state.total = 0;
        setEmpty(error.message || "运行记录加载失败。");
        renderStats({});
        renderPager();
        ui.showToast(error.message || "运行记录加载失败", true);
      }
    }
  }

  async function loadTemplates() {
    try {
      const payload = await ui.api(`${ui.API_BASE}/templates?limit=200&offset=0`);
      state.templates = ui.readItems(payload, ["templates", "items"]);
      renderTemplateOptions();
    } catch (_) {
      state.templates = [];
      renderTemplateOptions();
    }
  }

  function detailFact(label, value) {
    const card = ui.element("div", { className: "detail-card" });
    card.appendChild(ui.element("span", { text: label }));
    card.appendChild(ui.element("strong", { text: value == null || value === "" ? "—" : value }));
    return card;
  }

  function safeHttpsUrl(value) {
    try {
      const parsed = new URL(String(value || ""));
      const host = parsed.hostname.toLowerCase();
      const trustedHost = host === "x.com" || host.endsWith(".x.com") || host === "twitter.com" || host.endsWith(".twitter.com");
      return parsed.protocol === "https:" && trustedHost && !parsed.username && !parsed.password && (!parsed.port || parsed.port === "443") ? parsed.href : "";
    } catch (_) {
      return "";
    }
  }

  const PRIVATE_DETAIL_KEYS = new Set([
    "url",
    "media_url",
    "source_media_url",
    "prepared_media_url",
    "material_url",
    "gpu_media_url",
    "credential",
    "credentials",
    "access_token",
    "refresh_token",
  ]);
  const PUBLIC_DETAIL_URL_KEYS = new Set(["publish_url", "post_url", "x_post_url"]);

  function publicDetail(value) {
    if (Array.isArray(value)) return value.map(publicDetail);
    if (!value || typeof value !== "object") return value;
    const result = {};
    Object.entries(value).forEach(([key, item]) => {
      const normalized = String(key).toLowerCase();
      const privateUrl = normalized.endsWith("_url") && !PUBLIC_DETAIL_URL_KEYS.has(normalized);
      if (!PRIVATE_DETAIL_KEYS.has(normalized) && !privateUrl) result[key] = publicDetail(item);
    });
    return result;
  }

  function renderRunTasks(tasks) {
    const body = ui.byId("runTaskRows");
    ui.clear(body);
    if (!tasks.length) {
      const row = ui.element("tr");
      row.appendChild(ui.element("td", { className: "empty", text: "暂无账号任务。", attributes: { colspan: "6" } }));
      body.appendChild(row);
      return;
    }
    tasks.forEach(item => {
      const row = ui.element("tr");
      const accountCell = ui.element("td");
      accountCell.appendChild(ui.element("strong", { text: item.account_display_name || item.account_username || item.account_name || `账号 ${item.account_id || item.source_account_id || "—"}` }));
      accountCell.appendChild(ui.element("div", { className: "secondary mono", text: item.account_id || item.source_account_id || "—" }));
      row.appendChild(accountCell);

      const selection = ui.objectValue(item.selection);
      const dramaSelection = ui.objectValue(selection.drama);
      const materialSelection = ui.objectValue(selection.material);
      const selectionCell = ui.element("td");
      selectionCell.appendChild(ui.element("div", { text: `剧：${dramaSelection.name || dramaSelection.drama_name || item.drama_name || item.series_name || item.series_code || "—"}` }));
      selectionCell.appendChild(ui.element("div", { className: "secondary", text: `素材：${materialSelection.material_id || materialSelection.id || item.material_id || item.resource_id || "—"}` }));
      row.appendChild(selectionCell);

      const directMetrics = ui.objectValue(item.metrics || item.selected_metrics);
      const dramaMetrics = ui.objectValue(dramaSelection.metrics);
      const materialMetrics = ui.objectValue(materialSelection.metrics || directMetrics);
      const metricsCell = ui.element("td");
      const dramaRoas = dramaMetrics.d0_roas == null ? dramaMetrics.roas : dramaMetrics.d0_roas;
      const materialRoas = materialMetrics.d0_roas == null ? materialMetrics.roas : materialMetrics.d0_roas;
      metricsCell.appendChild(ui.element("div", { text: `剧 ROAS ${dramaRoas == null ? "—" : `${ui.formatNumber(dramaRoas)}%`} / 消耗 ${ui.formatNumber(dramaMetrics.spend)}` }));
      metricsCell.appendChild(ui.element("div", { className: "secondary", text: `素材 ROAS ${materialRoas == null ? "—" : `${ui.formatNumber(materialRoas)}%`} / 消耗 ${ui.formatNumber(materialMetrics.spend)}` }));
      row.appendChild(metricsCell);

      const stageCell = ui.element("td");
      const preparedDuration = item.selected_duration_sec == null ? item.prepared_duration_sec : item.selected_duration_sec;
      const prepareFact = item.prepare_status || item.preparation_status || (item.prepared ? `已准备 ${ui.formatNumber(preparedDuration)} 秒` : item.gpu_job_id ? `GPU ${item.gpu_job_id}` : "—");
      stageCell.appendChild(ui.element("div", { text: prepareFact }));
      const queueId = item.execution_queue_id || item.x_queue_id;
      const logId = item.execution_log_id || item.x_log_id;
      const ledgerFacts = [queueId ? `queue ${queueId}` : "", logId ? `log ${logId}` : ""].filter(Boolean).join(" / ");
      stageCell.appendChild(ui.element("div", { className: "secondary", text: item.publish_status || item.remote_status || ledgerFacts || (item.publish_id ? "已取得 X Post ID" : "—") }));
      row.appendChild(stageCell);

      const taskStatus = String(item.status || "").toLowerCase();
      const taskStatusCell = ui.element("td");
      taskStatusCell.appendChild(ui.statusBadge(statusLabel(taskStatus), ui.statusKind(taskStatus)));
      row.appendChild(taskStatusCell);

      const resultCell = ui.element("td");
      const publishId = item.x_post_id || item.post_id || item.publish_id || item.remote_publish_id;
      if (publishId) resultCell.appendChild(ui.element("div", { className: "mono", text: `X Post ID ${publishId}` }));
      const postUrl = safeHttpsUrl(item.x_post_url || item.post_url || item.publish_url);
      if (postUrl) {
        const link = ui.element("a", { text: "打开 X Post", href: postUrl, attributes: { target: "_blank", rel: "noopener noreferrer" } });
        resultCell.appendChild(link);
      }
      const error = item.error_message || item.last_error || item.rejection_reason;
      if (error) resultCell.appendChild(ui.element("div", { className: "secondary", text: error }));
      if (!publishId && !postUrl && !error) resultCell.appendChild(document.createTextNode("—"));
      row.appendChild(resultCell);
      body.appendChild(row);
    });
  }

  function renderEvents(events) {
    const container = ui.byId("runEvents");
    ui.clear(container);
    if (!events.length) {
      container.appendChild(ui.element("div", { className: "secondary", text: "暂无事件。" }));
      return;
    }
    events.forEach(item => {
      const event = ui.element("div", { className: "summary-item" });
      event.appendChild(ui.element("span", { text: ui.formatTime(item.created_at || item.occurred_at || item.timestamp) }));
      event.appendChild(ui.element("strong", { text: item.message || item.event_type || item.type || "事件" }));
      const details = item.details || item.data;
      if (details) event.appendChild(ui.element("div", { className: "secondary", text: typeof details === "string" ? details : JSON.stringify(publicDetail(details)) }));
      container.appendChild(event);
    });
  }

  function openDialog(dialog) {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function detailErrorMessage(error) {
    const labels = {
      x_auto_run_not_found: "运行记录不存在或已不可访问。",
      x_auto_template_not_found: "运行对应的模板版本不存在或已不可访问。",
      invalid_request: "运行详情请求无效。",
    };
    return labels[String(error && error.code || "")] || (error && error.message) || "详情加载失败。";
  }

  async function showRunDetail(id) {
    const request = ++state.detailRequest;
    const dialog = ui.byId("runDetailDialog");
    ui.setText(ui.byId("runDetailTitle"), `运行详情 · ${id}`, "");
    ui.setText(ui.byId("runDetailSubtitle"), "正在加载…", "");
    ui.clear(ui.byId("runDetailFacts"));
    renderRunTasks([]);
    ui.setText(ui.byId("runSnapshot"), "正在加载…", "");
    renderEvents([]);
    if (!dialog.open) openDialog(dialog);
    try {
      const payload = await ui.api(`${ui.API_BASE}/runs/${id}`);
      if (request !== state.detailRequest) return;
      const run = ui.readItem(payload, ["run", "item"]);
      const tasks = ui.readItems(payload, ["tasks", "account_tasks"]);
      const events = ui.readItems(payload, ["events"]);
      ui.setText(ui.byId("runDetailSubtitle"), `${run.template_name || `模板 ${run.template_id || "—"}`} · v${run.template_version || "—"}`, "");
      const facts = ui.byId("runDetailFacts");
      facts.append(
        detailFact("触发方式", triggerLabel(run.trigger_type)),
        detailFact("运行状态", statusLabel(run.status)),
        detailFact("计划 / 触发时间", ui.formatTime(run.scheduled_at || run.scheduled_at_utc || run.triggered_at || run.created_at)),
        detailFact("账号任务", `${tasks.length} 个`),
        detailFact("完成时间", ui.formatTime(run.completed_at || run.finished_at || run.finished_at_utc)),
        detailFact("运行 ID", id)
      );
      renderRunTasks(tasks);
      const snapshot = run.template_snapshot || run.rule_snapshot || payload.template_snapshot || payload.snapshot || run.config || {
        drama_rule: run.drama_rule,
        material_rule: run.material_rule,
        blacklist_snapshot: run.blacklist_snapshot,
      };
      ui.setText(ui.byId("runSnapshot"), JSON.stringify(publicDetail(snapshot), null, 2), "{}");
      renderEvents(events);
    } catch (error) {
      if (request !== state.detailRequest) return;
      const message = detailErrorMessage(error);
      ui.setText(ui.byId("runDetailSubtitle"), message, "");
      ui.setText(ui.byId("runSnapshot"), "详情加载失败。", "");
      ui.showToast(message, true);
    }
  }

  function syncPolling() {
    window.clearTimeout(state.pollTimer);
    const active = state.runs.some(item => ["queued", "scheduled", "running", "filtering", "preparing", "publishing", "reconciling"].includes(String(item.status || "").toLowerCase()));
    if (active) state.pollTimer = window.setTimeout(() => void loadRuns({ quiet: true }), 15000);
  }

  function closeDetail() {
    state.detailRequest += 1;
    const dialog = ui.byId("runDetailDialog");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function bindEvents() {
    ui.byId("runFilters").addEventListener("submit", event => {
      event.preventDefault();
      state.page = 1;
      void loadRuns();
    });
    ui.byId("resetFilters").addEventListener("click", () => {
      ui.byId("filterTemplateId").value = "";
      ui.byId("filterTriggerType").value = "";
      ui.byId("filterStatus").value = "";
      ui.byId("filterFrom").value = "";
      ui.byId("filterTo").value = "";
      state.page = 1;
      void loadRuns();
    });
    ui.byId("reloadRuns").addEventListener("click", () => void loadRuns());
    ui.byId("runRows").addEventListener("click", event => {
      const button = event.target.closest("button[data-run-id]");
      const id = ui.positiveId(button && button.dataset.runId);
      if (id) void showRunDetail(id);
    });
    ui.byId("prevPage").addEventListener("click", () => {
      if (state.page <= 1) return;
      state.page -= 1;
      void loadRuns();
    });
    ui.byId("nextPage").addEventListener("click", () => {
      if (state.page * state.pageSize >= state.total) return;
      state.page += 1;
      void loadRuns();
    });
    ui.byId("closeRunDetail").addEventListener("click", closeDetail);
    ui.byId("runDetailDialog").addEventListener("cancel", event => {
      event.preventDefault();
      closeDetail();
    });
    window.addEventListener("pagehide", () => window.clearTimeout(state.pollTimer));
  }

  void ui.initShell({
    activeKey: "xAutoPublishRuns",
    onReady: async () => {
      bindEvents();
      const params = new URLSearchParams(location.search);
      const templateFilter = ui.positiveId(params.get("template_id"));
      const directRunId = ui.positiveId(params.get("run_id"));
      await loadTemplates();
      if (templateFilter) ui.byId("filterTemplateId").value = templateFilter;
      await loadRuns();
      if (directRunId) void showRunDetail(directRunId);
    },
  });
})();
