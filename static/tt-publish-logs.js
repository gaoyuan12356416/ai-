(function () {
  "use strict";

  const ui = window.TtAutoPublish;
  const LOG_ENDPOINT = `${ui.API_BASE}/publish-logs`;
  const LEGACY_ENDPOINT = "/api/admin/tt-posts";
  const PAGE_SIZE = 20;
  const state = { items: [], offset: 0, total: 0, loading: false };

  const SOURCE_LABELS = {
    material_pool: "素材池发布",
    auto_template: "自动发布",
  };
  const TRIGGER_LABELS = {
    scheduled: "排期发布",
    direct_test: "立即测试",
    auto: "自动定时",
    manual: "手动执行",
  };
  const STATUS_LABELS = {
    scheduled: "等待执行",
    processing: "处理中",
    published: "已发布",
    needs_review: "结果待确认",
    failed: "失败",
    canceled: "已取消",
    no_candidate: "无可投素材",
    hold: "安全待处理",
    other: "其他",
  };

  function sourceChip(value) {
    return ui.element("span", {
      className: `source-chip ${value === "auto_template" ? "automatic" : "material"}`,
      text: SOURCE_LABELS[value] || value || "未知来源",
    });
  }

  function statusBadge(item) {
    const group = String(item.status_group || "other");
    const raw = String(item.status || "");
    const badge = ui.statusBadge(STATUS_LABELS[group] || raw || "未知", ui.statusKind(group === "processing" ? raw : group));
    const cell = ui.element("td");
    cell.appendChild(badge);
    if (raw && raw !== group) cell.appendChild(ui.element("div", { className: "secondary mono", text: raw }));
    return cell;
  }

  function primaryCell(primary, secondary) {
    const cell = ui.element("td");
    const wrapper = ui.element("div", { className: "primary-cell" });
    wrapper.appendChild(ui.element("strong", { text: primary || "—" }));
    if (secondary) wrapper.appendChild(ui.element("div", { className: "secondary", text: secondary }));
    cell.appendChild(wrapper);
    return cell;
  }

  function canCancel(item) {
    return item.publish_source === "material_pool"
      && item.trigger_type === "scheduled"
      && ["scheduled", "claimed"].includes(String(item.status || ""));
  }

  function canReconcile(item) {
    return item.publish_source === "material_pool"
      && item.trigger_type === "scheduled"
      && (ui.boolValue(item.unknown_outcome)
        || ["unknown", "reconciling"].includes(String(item.status || "")));
  }

  function renderRows() {
    const body = ui.byId("logRows");
    ui.clear(body);
    if (!state.items.length) {
      const row = ui.element("tr");
      const cell = ui.element("td", { className: "empty", text: "没有符合当前筛选条件的发布任务。" });
      cell.colSpan = 10;
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    state.items.forEach(item => {
      const row = ui.element("tr", { dataset: { taskKey: item.task_key } });
      row.appendChild(primaryCell(
        ui.formatTime(item.task_at_utc || item.scheduled_at_utc || item.created_at),
        `任务 ${item.task_id || "—"}`
      ));
      const sourceCell = ui.element("td");
      sourceCell.appendChild(sourceChip(item.publish_source));
      row.appendChild(sourceCell);
      ui.appendTextCell(row, TRIGGER_LABELS[item.trigger_type] || item.trigger_type || "—");
      row.appendChild(primaryCell(
        item.creator_nickname_snapshot || item.account_display_name || item.account_username || `账号 ${item.source_account_id || "—"}`,
        `${item.creator_username_snapshot ? `@${String(item.creator_username_snapshot).replace(/^@/, "")}` : "账号 ID"} ${item.source_account_id || "—"}`
      ));
      row.appendChild(primaryCell(
        item.template_name || (item.template_id ? `模板 ${item.template_id}` : "—"),
        item.run_id ? `运行 ${item.run_id} · v${item.template_version || "—"}` : ""
      ));
      row.appendChild(primaryCell(
        `素材 ${item.material_id || "—"}`,
        `Drama ID ${item.content_id || "—"}${item.drama_name ? ` · ${item.drama_name}` : ""}`
      ));
      ui.appendTextCell(row, item.caption || "—", "caption-copy");
      row.appendChild(statusBadge(item));

      const resultCell = ui.element("td", { className: "result-copy" });
      if (item.publish_id) resultCell.appendChild(ui.element("strong", { className: "mono", text: `publish_id ${item.publish_id}` }));
      if (item.publish_url) {
        resultCell.appendChild(ui.element("a", {
          className: "publish-link",
          text: "打开 TikTok 帖子",
          href: item.publish_url,
          attributes: { target: "_blank", rel: "noopener noreferrer" },
        }));
      }
      const error = [item.error_code, item.error_message].filter(Boolean).join(" · ");
      if (error) resultCell.appendChild(ui.element("div", { className: "secondary", text: error }));
      if (!resultCell.childNodes.length) resultCell.appendChild(document.createTextNode("—"));
      row.appendChild(resultCell);

      const actionCell = ui.element("td");
      const actions = ui.element("div", { className: "row-actions" });
      actions.appendChild(ui.element("button", {
        className: "button small",
        text: "查看详情",
        type: "button",
        dataset: { action: "details", taskKey: item.task_key },
      }));
      if (canCancel(item)) actions.appendChild(ui.element("button", {
        className: "button small danger",
        text: "取消任务",
        type: "button",
        dataset: { action: "cancel", taskKey: item.task_key },
      }));
      if (canReconcile(item)) actions.appendChild(ui.element("button", {
        className: "button small",
        text: "人工核对",
        type: "button",
        dataset: { action: "reconcile", taskKey: item.task_key },
      }));
      actionCell.appendChild(actions);
      row.appendChild(actionCell);
      body.appendChild(row);
    });
  }

  function updateSummary(payload) {
    const summary = ui.objectValue(payload.summary);
    const sources = ui.objectValue(payload.sources);
    ui.setText(ui.byId("totalCount"), summary.total || 0);
    ui.setText(ui.byId("scheduledCount"), summary.scheduled || 0);
    ui.setText(ui.byId("processingCount"), summary.processing || 0);
    ui.setText(ui.byId("reviewCount"), summary.needs_review || 0);
    ui.setText(ui.byId("publishedCount"), summary.published || 0);
    ui.setText(ui.byId("failedCount"), summary.failed || 0);
    ui.setText(ui.byId("materialSourceCount"), sources.material_pool || 0);
    ui.setText(ui.byId("autoSourceCount"), sources.auto_template || 0);
  }

  function setLoading(message) {
    const body = ui.byId("logRows");
    ui.clear(body);
    const row = ui.element("tr");
    const cell = ui.element("td", { className: "empty", text: message });
    cell.colSpan = 10;
    row.appendChild(cell);
    body.appendChild(row);
  }

  function filters() {
    return {
      publish_source: ui.byId("filterSource").value,
      trigger_type: ui.byId("filterTrigger").value,
      source_account_id: ui.byId("filterAccount").value,
      template_id: ui.byId("filterTemplate").value,
      material_id: ui.byId("filterMaterialId").value.trim(),
      content_id: ui.byId("filterContentId").value.trim(),
      status: ui.byId("filterStatus").value,
      from: ui.byId("filterFrom").value,
      to: ui.byId("filterTo").value,
      limit: PAGE_SIZE,
      offset: state.offset,
    };
  }

  function syncUrl() {
    const params = new URLSearchParams();
    Object.entries(filters()).forEach(([key, value]) => {
      if (!["limit", "offset"].includes(key) && value) params.set(key, value);
    });
    history.replaceState(null, "", `${location.pathname}${params.size ? `?${params}` : ""}`);
  }

  async function loadLogs(options) {
    if (state.loading) return;
    state.loading = true;
    const quiet = options && options.quiet;
    if (!quiet) setLoading("正在加载发布日志…");
    try {
      const payload = await ui.api(`${LOG_ENDPOINT}?${ui.queryString(filters())}`);
      state.items = ui.readItems(payload, ["items"]);
      const pagination = ui.objectValue(payload.pagination);
      state.total = ui.numberValue(pagination.total, state.items.length);
      renderRows();
      updateSummary(payload);
      const page = Math.floor(state.offset / PAGE_SIZE) + 1;
      const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
      ui.setText(ui.byId("pageInfo"), `第 ${page} / ${pages} 页，共 ${state.total} 条`);
      ui.byId("prevPage").disabled = state.offset <= 0;
      ui.byId("nextPage").disabled = state.offset + PAGE_SIZE >= state.total;
      syncUrl();
    } catch (error) {
      state.items = [];
      setLoading(`发布日志加载失败：${error.message}`);
      ["totalCount", "scheduledCount", "processingCount", "reviewCount", "publishedCount", "failedCount", "materialSourceCount", "autoSourceCount"]
        .forEach(id => ui.setText(ui.byId(id), "—"));
    } finally {
      state.loading = false;
    }
  }

  async function loadOptions() {
    const [accountsPayload, templatesPayload] = await Promise.all([
      ui.api(`${ui.API_BASE}/accounts`),
      ui.api(`${ui.API_BASE}/templates?limit=200&offset=0`),
    ]);
    const accountSelect = ui.byId("filterAccount");
    ui.readItems(accountsPayload, ["accounts", "items"]).forEach(item => {
      const id = ui.positiveId(item.source_account_id || item.account_id);
      if (!id) return;
      accountSelect.appendChild(ui.element("option", {
        text: `${item.creator_nickname || item.display_name || item.username || `账号 ${id}`} · ${id}`,
        attributes: { value: id },
      }));
    });
    const templateSelect = ui.byId("filterTemplate");
    ui.readItems(templatesPayload, ["templates", "items"]).forEach(item => {
      const id = ui.positiveId(item.id || item.template_id);
      if (!id) return;
      templateSelect.appendChild(ui.element("option", {
        text: `${item.name || `模板 ${id}`} · ${id}`,
        attributes: { value: id },
      }));
    });
  }

  function eventCard(event) {
    const card = ui.element("article", { className: "event-card" });
    const time = ui.element("div");
    time.appendChild(ui.element("strong", { text: ui.formatTime(event.created_at || event.occurred_at) }));
    time.appendChild(ui.element("span", { text: event.from_status && event.to_status ? `${event.from_status} → ${event.to_status}` : event.stage || "状态记录" }));
    const detail = ui.element("div");
    detail.appendChild(ui.element("strong", { text: event.event_type || event.status || "任务事件" }));
    const message = event.message || event.error_message || event.error_code || "状态已记录";
    detail.appendChild(ui.element("span", { text: message }));
    card.append(time, detail);
    return card;
  }

  function renderDetail(item, events, snapshot) {
    ui.setText(ui.byId("detailTitle"), `${SOURCE_LABELS[item.publish_source] || "发布"}任务 ${item.task_id}`);
    ui.setText(ui.byId("detailSubtitle"), `${TRIGGER_LABELS[item.trigger_type] || item.trigger_type} · ${ui.formatTime(item.task_at_utc)}`);
    const facts = ui.byId("detailFacts");
    ui.clear(facts);
    [
      ["发布来源", SOURCE_LABELS[item.publish_source]],
      ["触发方式", TRIGGER_LABELS[item.trigger_type]],
      ["账号", item.account_display_name || item.creator_nickname_snapshot || item.source_account_id],
      ["素材 / Drama ID", `${item.material_id || "—"} / ${item.content_id || "—"}`],
      ["模板 / 运行", item.template_id ? `${item.template_name || `模板 ${item.template_id}`} / ${item.run_id || "—"}` : "—"],
      ["状态", `${STATUS_LABELS[item.status_group] || item.status_group} (${item.status || "—"})`],
      ["publish_id", item.publish_id || "—"],
      ["错误", [item.error_code, item.error_message].filter(Boolean).join(" · ") || "—"],
    ].forEach(([label, value]) => {
      const fact = ui.element("div", { className: "detail-fact" });
      fact.appendChild(ui.element("span", { text: label }));
      fact.appendChild(ui.element("strong", { text: value || "—" }));
      facts.appendChild(fact);
    });
    const list = ui.byId("detailEvents");
    ui.clear(list);
    if (!events.length) list.appendChild(ui.element("div", { className: "empty", text: "暂无事件记录。" }));
    else events.forEach(event => list.appendChild(eventCard(event)));
    ui.setText(ui.byId("detailSnapshotTitle"), item.publish_source === "auto_template" ? "自动发布运行快照" : "任务快照");
    ui.setText(ui.byId("detailSnapshot"), JSON.stringify(snapshot || item, null, 2), "—");
    const dialog = ui.byId("detailDialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function directTimeline(item) {
    const events = [];
    const add = (time, type, stage, message) => {
      if (time) events.push({ created_at: time, event_type: type, stage, message: message || "" });
    };
    add(item.created_at, "立即测试已创建", "queued", "任务进入独立测试流程");
    add(item.prepared_at_utc, "成片制作完成", "ready");
    add(item.publish_started_at_utc, "已提交 TikTok", "publishing");
    add(item.published_at_utc, "TikTok 发布完成", "published");
    add(item.failed_at_utc, "立即测试失败", "failed", item.error_message || item.error_code);
    add(item.canceled_at_utc, "立即测试已取消", "canceled");
    return events.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  }

  async function openDetails(item) {
    if (item.publish_source === "auto_template") {
      const payload = await ui.api(`${ui.API_BASE}/runs/${encodeURIComponent(item.run_id)}`);
      const events = ui.readItems(payload, ["events"]).filter(event => !event.task_id || String(event.task_id) === String(item.task_id));
      const task = ui.readItems(payload, ["tasks"]).find(value => String(value.id || value.task_id) === String(item.task_id));
      renderDetail(item, events, { run: payload.run || {}, task: task || item });
      return;
    }
    if (item.trigger_type === "direct_test") {
      renderDetail(item, directTimeline(item), item);
      return;
    }
    const payload = await ui.api(`${LEGACY_ENDPOINT}/events?${ui.queryString({ queue_id: item.task_id })}`);
    renderDetail(item, ui.readItems(payload, ["items", "events"]), item);
  }

  async function queueAction(item, action, button) {
    const isCancel = action === "cancel";
    const confirmed = await ui.confirmAction({
      title: isCancel ? "取消素材池发布任务" : "人工核对发布结果",
      message: isCancel
        ? `确定取消任务 ${item.task_id}？取消后不会继续发布。`
        : `确定核对任务 ${item.task_id}？只查询已有结果，不会创建第二条发布请求。`,
      confirmText: isCancel ? "确认取消" : "开始核对",
      danger: isCancel,
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      await ui.api(`${LEGACY_ENDPOINT}/queue/${encodeURIComponent(item.task_id)}/${action}`, {
        method: "POST",
        body: JSON.stringify(isCancel ? { reason: "由TT发布日志人工取消" } : {}),
      });
      ui.showToast(isCancel ? "任务已取消。" : "人工核对完成。", false);
      await loadLogs({ quiet: true });
    } catch (error) {
      ui.showToast(`${isCancel ? "取消" : "核对"}失败：${error.message}`, true);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }

  function applyInitialQuery() {
    const params = new URLSearchParams(location.search);
    const mappings = {
      publish_source: "filterSource",
      trigger_type: "filterTrigger",
      source_account_id: "filterAccount",
      template_id: "filterTemplate",
      material_id: "filterMaterialId",
      content_id: "filterContentId",
      status: "filterStatus",
      from: "filterFrom",
      to: "filterTo",
    };
    Object.entries(mappings).forEach(([key, id]) => {
      const value = params.get(key);
      const node = ui.byId(id);
      if (value && node) node.value = value;
    });
    return ui.positiveId(params.get("run_id"));
  }

  async function openInitialRun(runId) {
    if (!runId) return;
    try {
      const payload = await ui.api(`${ui.API_BASE}/runs/${encodeURIComponent(runId)}`);
      const tasks = ui.readItems(payload, ["tasks"]);
      const task = tasks[0];
      if (!task) return;
      const matching = state.items.find(item => String(item.run_id) === String(runId) && String(item.task_id) === String(task.id || task.task_id));
      if (matching) await openDetails(matching);
    } catch (error) {
      ui.showToast(`运行详情加载失败：${error.message}`, true);
    }
  }

  async function ready() {
    await loadOptions();
    const runId = applyInitialQuery();
    await loadLogs();
    await openInitialRun(runId);
  }

  ui.byId("logFilters").addEventListener("submit", event => {
    event.preventDefault();
    state.offset = 0;
    void loadLogs();
  });
  ui.byId("resetFilters").addEventListener("click", () => {
    ui.byId("logFilters").reset();
    state.offset = 0;
    void loadLogs();
  });
  ui.byId("reloadLogs").addEventListener("click", () => void loadLogs());
  ui.byId("prevPage").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - PAGE_SIZE);
    void loadLogs();
  });
  ui.byId("nextPage").addEventListener("click", () => {
    if (state.offset + PAGE_SIZE < state.total) {
      state.offset += PAGE_SIZE;
      void loadLogs();
    }
  });
  ui.byId("logRows").addEventListener("click", event => {
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const item = state.items.find(value => value.task_key === button.dataset.taskKey);
    if (!item) return;
    if (button.dataset.action === "details") {
      void openDetails(item).catch(error => ui.showToast(`详情加载失败：${error.message}`, true));
    } else if (["cancel", "reconcile"].includes(button.dataset.action)) {
      void queueAction(item, button.dataset.action, button);
    }
  });
  ui.byId("closeDetail").addEventListener("click", () => ui.byId("detailDialog").close());
  ui.byId("detailDialog").addEventListener("click", event => {
    if (event.target === ui.byId("detailDialog")) ui.byId("detailDialog").close();
  });
  void ui.initShell({ activeKey: "ttAutoPublishRuns", onReady: ready });
})();
