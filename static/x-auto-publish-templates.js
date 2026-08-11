(function () {
  "use strict";

  const ui = window.XAutoPublish;
  const state = {
    page: 1,
    pageSize: 20,
    total: 0,
    templates: [],
    busyIds: new Set(),
  };

  function templateId(item) {
    return ui.positiveId(item && (item.id || item.template_id));
  }

  function templateVersion(item) {
    return Math.max(0, ui.numberValue(item && (item.version || item.current_version), 0));
  }

  function isEnabled(item) {
    return ui.boolValue(item && item.enabled) || String(item && item.status || "").toLowerCase() === "enabled";
  }

  function manualRunStorageKey(id, version) {
    return `x-auto-run-now:${id}:v${version}`;
  }

  function manualRunIdempotencyKey(id, version) {
    const storageKey = manualRunStorageKey(id, version);
    try {
      const existing = window.sessionStorage.getItem(storageKey);
      if (/^[A-Za-z0-9._:@-]{8,128}$/.test(existing || "")) return existing;
    } catch (_) {
      // Continue with an in-memory request key when storage is unavailable.
    }
    let nonce = "";
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      nonce = window.crypto.randomUUID();
    } else if (window.crypto && typeof window.crypto.getRandomValues === "function") {
      const values = new Uint32Array(4);
      window.crypto.getRandomValues(values);
      nonce = Array.from(values, value => value.toString(16).padStart(8, "0")).join("");
    } else {
      nonce = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    const value = `ui-${id}-v${version}-${nonce}`.slice(0, 128);
    try {
      window.sessionStorage.setItem(storageKey, value);
    } catch (_) {
      state.manualRunKeys = state.manualRunKeys || new Map();
      const remembered = state.manualRunKeys.get(storageKey);
      if (remembered) return remembered;
      state.manualRunKeys.set(storageKey, value);
    }
    return value;
  }

  function forgetManualRunKey(id, version) {
    const storageKey = manualRunStorageKey(id, version);
    try { window.sessionStorage.removeItem(storageKey); } catch (_) {}
    if (state.manualRunKeys) state.manualRunKeys.delete(storageKey);
  }

  function scheduleSummary(item) {
    if (item && item.schedule_summary) return String(item.schedule_summary);
    const config = ui.objectValue(item && item.config);
    const schedule = ui.objectValue(item && item.schedule || config.schedule);
    if (schedule.mode === "random") {
      const count = ui.numberValue(schedule.daily_count || schedule.random_daily_count, 0);
      return count ? `每天随机 ${count} 次` : "随机计划未配置";
    }
    const times = Array.isArray(schedule.times) ? schedule.times : [];
    return times.length ? `每天 ${times.join("、")}` : "固定时间未配置";
  }

  function sortLabel(rule) {
    const item = ui.objectValue(rule);
    const metric = item.sort_by === "spend" ? "消耗" : "D0 ROAS";
    const direction = item.sort_direction === "asc" ? "正序" : "倒序";
    return `${metric} ${direction}`;
  }

  function accountCount(item) {
    if (Number.isFinite(Number(item && item.account_count))) return Number(item.account_count);
    if (Array.isArray(item && item.account_ids)) return item.account_ids.length;
    if (Array.isArray(item && item.accounts)) return item.accounts.length;
    if (Array.isArray(item && item.config && item.config.account_ids)) return item.config.account_ids.length;
    return 0;
  }

  function statusLabel(status, enabled) {
    const value = String(status || "").toLowerCase();
    if (enabled) return "已启用";
    if (value === "archived") return "已归档";
    return "已停用";
  }

  function runStatusLabel(value) {
    const labels = {
      queued: "等待执行",
      scheduled: "等待执行",
      running: "执行中",
      preparing: "准备中",
      publishing: "发布中",
      completed: "已完成",
      published: "已发布",
      failed: "失败",
      partial_failed: "部分失败",
      needs_review: "结果待确认",
      canceled: "已取消",
    };
    const normalized = String(value || "").toLowerCase();
    return labels[normalized] || value || "暂无运行";
  }

  function actionButton(label, action, id, kind) {
    return ui.element("button", {
      className: `button small${kind ? ` ${kind}` : ""}`,
      text: label,
      type: "button",
      dataset: { action, templateId: id },
    });
  }

  function setEmpty(message) {
    const body = ui.byId("templateRows");
    ui.clear(body);
    const row = ui.element("tr");
    const cell = ui.element("td", { className: "empty", text: message, attributes: { colspan: "8" } });
    row.appendChild(cell);
    body.appendChild(row);
  }

  function renderStats(payload) {
    const summary = ui.objectValue(payload && (payload.summary || payload.counts));
    const enabled = ui.numberValue(summary.enabled, state.templates.filter(isEnabled).length);
    const disabled = ui.numberValue(summary.disabled, Math.max(0, state.total - enabled));
    const running = ui.numberValue(summary.running || summary.active_runs, state.templates.filter(item => {
      const status = String(item.last_run_status || (item.last_run && item.last_run.status) || "").toLowerCase();
      return ["queued", "running", "preparing", "publishing"].includes(status);
    }).length);
    ui.setText(ui.byId("totalCount"), state.total, "0");
    ui.setText(ui.byId("enabledCount"), enabled, "0");
    ui.setText(ui.byId("disabledCount"), disabled, "0");
    ui.setText(ui.byId("runningCount"), running, "0");
  }

  function renderTemplates() {
    const body = ui.byId("templateRows");
    ui.clear(body);
    if (!state.templates.length) {
      setEmpty("没有符合条件的模板。");
      return;
    }

    state.templates.forEach(item => {
      const id = templateId(item);
      const enabled = isEnabled(item);
      const version = templateVersion(item);
      const row = ui.element("tr");

      const nameCell = ui.element("td");
      const primary = ui.element("div", { className: "primary-cell" });
      primary.appendChild(ui.element("strong", { text: item.name || `模板 ${id}` }));
      primary.appendChild(ui.element("span", { className: "secondary mono", text: `ID ${id || "—"} · v${version}` }));
      nameCell.appendChild(primary);
      row.appendChild(nameCell);

      const statusCell = ui.element("td");
      statusCell.appendChild(ui.statusBadge(statusLabel(item.status, enabled), enabled ? "success" : "warning"));
      row.appendChild(statusCell);

      const config = ui.objectValue(item.config);
      const accountsCell = ui.element("td");
      accountsCell.appendChild(ui.element("strong", { text: `${accountCount(item)} 个账号` }));
      const languageSummary = item.language || config.language;
      if (languageSummary) accountsCell.appendChild(ui.element("div", { className: "secondary", text: languageSummary }));
      row.appendChild(accountsCell);

      ui.appendTextCell(row, scheduleSummary(item));

      const sortCell = ui.element("td");
      sortCell.appendChild(ui.element("div", { text: `剧：${sortLabel(item.drama_rule || config.drama_rule)}` }));
      sortCell.appendChild(ui.element("div", { className: "secondary", text: `素材：${sortLabel(item.material_rule || config.material_rule)}` }));
      row.appendChild(sortCell);

      ui.appendTextCell(row, ui.formatTime(item.next_run_at || item.next_scheduled_at));

      const lastRun = ui.objectValue(item.last_run);
      const lastStatus = item.last_run_status || lastRun.status;
      const lastCell = ui.element("td");
      lastCell.appendChild(ui.statusBadge(runStatusLabel(lastStatus), ui.statusKind(lastStatus)));
      const lastTime = item.last_run_at || lastRun.created_at || lastRun.triggered_at;
      if (lastTime) lastCell.appendChild(ui.element("div", { className: "secondary", text: ui.formatTime(lastTime) }));
      row.appendChild(lastCell);

      const actionsCell = ui.element("td");
      const actions = ui.element("div", { className: "row-actions" });
      actions.appendChild(actionButton("编辑", "edit", id));
      actions.appendChild(actionButton("复制", "copy", id));
      actions.appendChild(actionButton("预览", "preview", id));
      actions.appendChild(actionButton(enabled ? "关闭" : "开启", enabled ? "disable" : "enable", id, enabled ? "danger" : ""));
      actions.appendChild(actionButton("手动执行", "run", id, "primary"));
      actions.appendChild(actionButton("记录", "runs", id));
      if (state.busyIds.has(id)) actions.querySelectorAll("button").forEach(button => { button.disabled = true; });
      actionsCell.appendChild(actions);
      row.appendChild(actionsCell);
      body.appendChild(row);
    });
  }

  function renderPager() {
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    ui.setText(ui.byId("pageInfo"), `第 ${state.page} / ${pages} 页，共 ${state.total} 条`, "");
    ui.byId("prevPage").disabled = state.page <= 1;
    ui.byId("nextPage").disabled = state.page >= pages;
  }

  async function loadTemplates() {
    setEmpty("正在加载模板…");
    const query = ui.queryString({
      q: ui.byId("filterQuery").value.trim(),
      status: ui.byId("filterStatus").value,
      limit: state.pageSize,
      offset: (state.page - 1) * state.pageSize,
    });
    try {
      const payload = await ui.api(`${ui.API_BASE}/templates?${query}`);
      state.templates = ui.readItems(payload, ["templates", "items"]);
      state.total = Math.max(0, ui.numberValue(payload.total || (payload.pagination && payload.pagination.total), state.templates.length));
      renderTemplates();
      renderStats(payload);
      renderPager();
    } catch (error) {
      state.templates = [];
      state.total = 0;
      setEmpty(error.message || "模板加载失败。");
      renderStats({});
      renderPager();
      ui.showToast(error.message || "模板加载失败", true);
    }
  }

  function findTemplate(id) {
    return state.templates.find(item => templateId(item) === id) || null;
  }

  async function previewTemplate(item) {
    const id = templateId(item);
    const version = templateVersion(item);
    state.busyIds.add(id);
    renderTemplates();
    try {
      const payload = await ui.api(`${ui.API_BASE}/templates/${id}/preview`, {
        method: "POST",
        body: JSON.stringify({ expected_version: version }),
      });
      const previews = ui.readItems(payload, ["preview", "items"]);
      const lines = previews.slice(0, 20).map(result => {
        const account = result.account_name || result.account_display_name || result.account_id || "未知账号";
        if (!ui.boolValue(result.ok)) return `${account}：${result.error_message || result.error_code || "没有可用素材"}`;
        const selection = ui.objectValue(result.selection);
        const drama = ui.objectValue(selection.drama);
        const material = ui.objectValue(selection.material);
        return `${account}：${drama.name || drama.drama_name || "已选剧"} / 素材 ${material.material_id || material.id || "已选"}`;
      });
      const message = lines.length
        ? `${lines.join("\n")}${previews.length > lines.length ? `\n另有 ${previews.length - lines.length} 个账号未展开` : ""}`
        : "当前模板没有可预览的账号结果。";
      await ui.confirmAction({
        title: "只读选材预览",
        message,
        confirmText: "关闭",
      });
    } catch (error) {
      ui.showToast(error.message || "模板预览失败", true);
    } finally {
      state.busyIds.delete(id);
      renderTemplates();
    }
  }

  async function mutateTemplate(item, action) {
    const id = templateId(item);
    const version = templateVersion(item);
    const names = {
      copy: "复制模板",
      enable: "开启模板",
      disable: "关闭模板",
      run: "手动执行模板",
    };
    const messages = {
      copy: `确认复制“${item.name || id}”吗？复制出的模板默认关闭。`,
      enable: `确认开启“${item.name || id}”吗？开启后系统会按模板计划自动创建真实发布任务。`,
      disable: `确认关闭“${item.name || id}”吗？关闭只停止后续自动触发，不会取消已经开始的任务。`,
      run: `确认立即执行“${item.name || id}”吗？系统会为模板内每个账号创建一次真实发布任务，视频准备完成后自动发布。`,
    };
    const confirmed = await ui.confirmAction({
      title: names[action],
      message: messages[action],
      confirmText: action === "run" ? "确认真实执行" : "确认",
      danger: action === "disable" || action === "run",
    });
    if (!confirmed) return;
    state.busyIds.add(id);
    renderTemplates();
    try {
      const body = { expected_version: version };
      if (action === "run") {
        body.confirmed = true;
        body.idempotency_key = manualRunIdempotencyKey(id, version);
      }
      const payload = await ui.api(`${ui.API_BASE}/templates/${id}/${action === "run" ? "run-now" : action}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (action === "run") {
        const runId = ui.positiveId(payload.run_id || (payload.run && payload.run.id));
        forgetManualRunKey(id, version);
        ui.showToast("手动执行任务已创建。", false);
        location.href = runId ? `/x-auto-publish-runs.html?run_id=${encodeURIComponent(runId)}` : "/x-auto-publish-runs.html";
        return;
      }
      if (action === "copy") {
        const copied = ui.readItem(payload, ["template", "item"]);
        const copiedId = ui.positiveId(copied.id || copied.template_id || payload.template_id);
        ui.showToast("模板已复制，新模板保持关闭。", false);
        if (copiedId) {
          location.href = `/x-auto-publish-template.html?id=${encodeURIComponent(copiedId)}`;
          return;
        }
      } else {
        ui.showToast(action === "enable" ? "模板已开启。" : "模板已关闭。", false);
      }
      await loadTemplates();
    } catch (error) {
      if (
        action === "run"
        && Number(error.status || 0) >= 400
        && Number(error.status || 0) < 500
      ) {
        forgetManualRunKey(id, version);
      }
      ui.showToast(error.message || `${names[action]}失败`, true);
    } finally {
      state.busyIds.delete(id);
      renderTemplates();
    }
  }

  async function handleTableAction(event) {
    const button = event.target.closest("button[data-action][data-template-id]");
    if (!button || !ui.byId("templateRows").contains(button)) return;
    const id = ui.positiveId(button.dataset.templateId);
    const item = findTemplate(id);
    if (!item || state.busyIds.has(id)) return;
    const action = button.dataset.action;
    if (action === "edit") {
      location.href = `/x-auto-publish-template.html?id=${encodeURIComponent(id)}`;
      return;
    }
    if (action === "runs") {
      location.href = `/x-auto-publish-runs.html?template_id=${encodeURIComponent(id)}`;
      return;
    }
    if (action === "preview") {
      await previewTemplate(item);
      return;
    }
    if (["copy", "enable", "disable", "run"].includes(action)) await mutateTemplate(item, action);
  }

  function bindEvents() {
    ui.byId("templateFilters").addEventListener("submit", event => {
      event.preventDefault();
      state.page = 1;
      void loadTemplates();
    });
    ui.byId("resetFilters").addEventListener("click", () => {
      ui.byId("filterQuery").value = "";
      ui.byId("filterStatus").value = "";
      state.page = 1;
      void loadTemplates();
    });
    ui.byId("reloadTemplates").addEventListener("click", () => void loadTemplates());
    ui.byId("templateRows").addEventListener("click", event => void handleTableAction(event));
    ui.byId("prevPage").addEventListener("click", () => {
      if (state.page <= 1) return;
      state.page -= 1;
      void loadTemplates();
    });
    ui.byId("nextPage").addEventListener("click", () => {
      if (state.page * state.pageSize >= state.total) return;
      state.page += 1;
      void loadTemplates();
    });
  }

  void ui.initShell({
    activeKey: "xAutoPublishTemplates",
    onReady: async () => {
      bindEvents();
      await loadTemplates();
    },
  });
})();
