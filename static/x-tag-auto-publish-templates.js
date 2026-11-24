(function () {
  "use strict";

  const ui = window.XAutoPublish;
  const state = {
    page: 1,
    pageSize: 20,
    total: 0,
    templates: [],
    busyIds: new Set(),
    summary: {},
    operatorName: "",
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

  function pendingRunKey(id, version) {
    const storageKey = manualRunStorageKey(id, version);
    try {
      const existing = window.sessionStorage.getItem(storageKey);
      if (/^[A-Za-z0-9._:@-]{8,128}$/.test(existing || "")) return existing;
    } catch (_) {}
    return state.manualRunKeys?.get(storageKey) || "";
  }

  function manualRunIdempotencyKey(id, version, model) {
    const storageKey = manualRunStorageKey(id, version);
    const pending = pendingRunKey(id, version);
    if (pending) return pending;
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
    const value = `ui-${id}-v${version}-${model}-${nonce}`.slice(0, 128);
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
    const cell = ui.element("td", { className: "empty", text: message, attributes: { colspan: "9" } });
    row.appendChild(cell);
    body.appendChild(row);
  }

  function renderStats(payload) {
    const summary = ui.objectValue(payload && (payload.summary || payload.counts));
    const enabled = ui.numberValue(summary.enabled, state.templates.filter(isEnabled).length);
    const disabled = ui.numberValue(summary.disabled, Math.max(0, state.total - enabled));
    const running = ui.numberValue(summary.running ?? summary.active_runs, state.templates.filter(item => {
      const status = String(item.last_run_status || (item.last_run && item.last_run.status) || "").toLowerCase();
      return ["queued", "running", "preparing", "publishing"].includes(status);
    }).length);
    state.summary = { enabled, disabled, running };
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

    state.templates.forEach(item => body.appendChild(templateRow(item)));
  }

  function templateRow(item) {
    const config = ui.objectValue(item && item.config);
    const id = templateId(item);
    const enabled = isEnabled(item);
    const version = templateVersion(item);
    const row = ui.element("tr", { dataset: { templateId: id } });

    const nameCell = ui.element("td");
    const primary = ui.element("div", { className: "primary-cell" });
    primary.appendChild(ui.element("strong", { text: item.name || `模板 ${id}` }));
    primary.appendChild(ui.element("span", { className: "secondary mono", text: `ID ${id || "—"} · v${version}` }));
    nameCell.appendChild(primary);
    row.appendChild(nameCell);

    const statusCell = ui.element("td");
    const statusControl = ui.element("div", { className: "template-status-control" });
    statusControl.appendChild(ui.statusBadge(statusLabel(item.status, enabled), enabled ? "success" : "warning"));
    const toggle = ui.element("button", {
      type: "button",
      className: "template-status-switch",
      title: enabled ? "点击关闭自动发布" : "点击开启自动发布",
      attributes: { role: "switch", "aria-checked": String(enabled), "aria-label": `${item.name || id} 自动发布`, "aria-busy": String(state.busyIds.has(id)) },
      dataset: { action: enabled ? "disable" : "enable", templateId: id },
    });
    toggle.disabled = state.busyIds.has(id);
    statusControl.appendChild(toggle);
    statusCell.appendChild(statusControl);
    row.appendChild(statusCell);

    const accountsCell = ui.element("td");
    accountsCell.appendChild(ui.element("strong", { text: `${accountCount(item)} 个账号` }));
    const profileModel = config.profile_model || item.profile_model;
    accountsCell.appendChild(ui.element("div", { className: "secondary", text: profileModel === "growth" ? "成长画像" : "收入画像" }));
    row.appendChild(accountsCell);

    ui.appendTextCell(row, scheduleSummary(item));

    const sortCell = ui.element("td");
    sortCell.appendChild(ui.element("div", { text: `剧：${sortLabel(item.drama_rule || config.drama_rule)}` }));
    sortCell.appendChild(ui.element("div", { className: "secondary", text: `素材：${sortLabel(item.material_rule || config.material_rule)}` }));
    row.appendChild(sortCell);

    ui.appendTextCell(row, ui.formatTime(item.next_run_at || item.next_scheduled_at));

    const lastRun = ui.objectValue(item.last_run);
    const lastStatus = lastRun.display_status || item.last_run_status || lastRun.status;
    const lastCell = ui.element("td");
    lastCell.appendChild(ui.statusBadge(ui.runLabel(lastStatus), ui.statusKind(lastStatus)));
    const lastTime = item.last_run_at || lastRun.created_at || lastRun.triggered_at;
    if (lastRun.task_counts) lastCell.appendChild(ui.element("div", { text: ui.runSummary(lastRun) }));
    if (lastTime) lastCell.appendChild(ui.element("div", { className: "secondary", text: "触发 " + ui.formatTime(lastTime) }));
    if (lastRun.last_progress_at_utc) lastCell.appendChild(ui.element("div", { className: "secondary", text: "进展 " + ui.formatTime(lastRun.last_progress_at_utc) }));
    row.appendChild(lastCell);

    ui.appendTextCell(row, item.operator_name || "—");

    const actionsCell = ui.element("td");
    const actions = ui.element("div", { className: "template-actions" });
    actions.appendChild(actionButton("编辑", "edit", id));
    actions.appendChild(actionButton("记录", "runs", id));
    actions.appendChild(actionButton("手动执行", "run", id, "primary"));
    const more = ui.element("details");
    more.appendChild(ui.element("summary", { className: "button small", text: "更多操作", attributes: { "aria-label": `${item.name || id} 更多操作` } }));
    const options = ui.element("div", { className: "template-action-options" });
    options.appendChild(actionButton("复制", "copy", id));
    options.appendChild(actionButton("爆款素材发布", "material", id));
    options.appendChild(actionButton("爆款剧发布", "drama", id));
    options.appendChild(actionButton("删除", "delete", id, "danger"));
    more.appendChild(options);
    actions.appendChild(more);
    if (state.busyIds.has(id)) actions.querySelectorAll("button").forEach(button => { button.disabled = true; });
    actionsCell.appendChild(actions);
    row.appendChild(actionsCell);
    return row;
  }

  function setRowBusy(id, busy) {
    const row = ui.byId("templateRows").querySelector(`tr[data-template-id="${id}"]`);
    if (!row) return;
    row.querySelectorAll("button").forEach(button => { button.disabled = busy; });
    row.querySelector('[role="switch"]')?.setAttribute("aria-busy", String(busy));
  }

  function applyTemplateUpdate(next, operatorName) {
    const id = templateId(next);
    const index = state.templates.findIndex(item => templateId(item) === id);
    if (index < 0) return;
    const previous = state.templates[index];
    const current = { ...previous, ...next };
    current.next_run_at = next.next_run_at || next.next_scheduled_at || "";
    current.next_scheduled_at = "";
    if (operatorName) current.operator_name = operatorName;
    state.templates[index] = current;
    const delta = Number(isEnabled(current)) - Number(isEnabled(previous));
    renderStats({ summary: { ...state.summary, enabled: state.summary.enabled + delta, disabled: state.summary.disabled - delta } });
    const row = ui.byId("templateRows").querySelector(`tr[data-template-id="${id}"]`);
    if (row) {
      const expanded = !!row.querySelector("details[open]");
      const hadFocus = row.contains(document.activeElement);
      const replacement = templateRow(current);
      if (expanded) replacement.querySelector("details").open = true;
      row.replaceWith(replacement);
      if (hadFocus) replacement.querySelector('[role="switch"]')?.focus({ preventScroll: true });
    }
  }

  async function toggleTemplate(item, action) {
    const id = templateId(item);
    state.busyIds.add(id);
    setRowBusy(id, true);
    try {
      const payload = await ui.api(`${ui.API_BASE}/templates/${id}/${action}`, {
        method: "POST", body: JSON.stringify({ expected_version: templateVersion(item) }),
      });
      const updated = ui.readItem(payload, ["template", "item"]);
      if (templateId(updated) !== id) throw new Error("模板状态返回不完整，正在核对。");
      state.busyIds.delete(id);
      applyTemplateUpdate(updated, updated.operator_name || state.operatorName);
      ui.showToast(isEnabled(updated) ? "模板已开启。" : "模板已关闭。", false);
    } catch (error) {
      try {
        const payload = await ui.api(`${ui.API_BASE}/templates/${id}`);
        const updated = ui.readItem(payload, ["template", "item"]);
        if (templateId(updated) !== id) throw new Error("状态读取失败");
        state.busyIds.delete(id);
        applyTemplateUpdate(updated);
      } catch (_) {
        // Keep the last confirmed row; never clear unrelated rows or retry a write.
      }
      ui.showToast(error.message || "状态更新失败，请核对后重试。", true);
    } finally {
      state.busyIds.delete(id);
      setRowBusy(id, false);
    }
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
      if (payload.operator_metadata_unavailable) ui.showToast("操作人信息暂不可用，请稍后刷新。", true);
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

  async function mutateTemplate(item, action) {
    const id = templateId(item);
    const version = templateVersion(item);
    if (action === "enable" || action === "disable") return toggleTemplate(item, action);
    const names = {
      copy: "复制模板",
      delete: "删除模板",
      run: "手动执行模板",
    };
    const messages = {
      copy: `确认复制“${item.name || id}”吗？复制出的模板默认关闭。`,
      delete: `确认删除“${item.name || id}”吗？删除后将从模板列表移除并停止新调度；已生成任务和历史发布记录保留。`,
      run: `确认立即执行“${item.name || id}”吗？系统会为模板内每个账号创建一次真实发布任务，视频准备完成后自动发布。`,
    };
    let picker, pendingKey, pendingModel;
    if (action === "run") {
      pendingKey = pendingRunKey(id, version);
      pendingModel = pendingKey.match(/^ui-\d+-v\d+-(growth|revenue)-/)?.[1];
      picker = window.XPublishProfileModel.field(pendingModel || item.config?.profile_model || item.profile_model);
      if (pendingKey) {
        picker.select.disabled = true;
        if (!pendingModel) {
          picker.select.add(new Option("沿用上次提交", "legacy"));
          picker.select.value = "legacy";
        }
        messages.run = "上一次提交的结果待确认。继续执行会使用同一操作编号核对结果。";
      }
      ui.byId("confirmMessage").after(picker.group);
    }
    const confirmed = await ui.confirmAction({
      title: names[action],
      message: messages[action],
      confirmText: action === "run" ? "确认真实执行" : "确认",
      danger: action === "delete" || action === "run",
    });
    picker?.group.remove();
    if (!confirmed) return;
    state.busyIds.add(id);
    renderTemplates();
    try {
      const body = { expected_version: version };
      if (action === "run") {
        body.confirmed = true;
        if (!pendingKey || pendingModel) body.profile_model = picker.select.value;
        body.idempotency_key = manualRunIdempotencyKey(id, version, picker.select.value);
      }
      const payload = await ui.api(`${ui.API_BASE}/templates/${id}/${action === "run" ? "run-now" : action}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (action === "run") {
        const runId = ui.positiveId(payload.run_id || (payload.run && payload.run.id));
        forgetManualRunKey(id, version);
        ui.showToast("手动执行任务已创建。", false);
        location.href = runId ? `/x-tag-auto-publish-runs.html?run_id=${encodeURIComponent(runId)}` : "/x-tag-auto-publish-runs.html";
        return;
      }
      if (action === "copy") {
        const copied = ui.readItem(payload, ["template", "item"]);
        const copiedId = ui.positiveId(copied.id || copied.template_id || payload.template_id);
        ui.showToast("模板已复制，新模板保持关闭。", false);
        if (copiedId) {
          location.href = `/x-tag-auto-publish-template.html?id=${encodeURIComponent(copiedId)}`;
          return;
        }
      } else if (action === "delete") {
        ui.showToast("模板已删除，已生成任务和历史记录保留。", false);
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
    if (action === "material") { await window.HitMaterialPublish.open(id); return; }
    if (action === "drama") { await window.HitDramaPublish.open("x", id); return; }
    if (action === "edit") {
      location.href = `/x-tag-auto-publish-template.html?id=${encodeURIComponent(id)}`;
      return;
    }
    if (action === "runs") {
      location.href = `/x-tag-auto-publish-runs.html?template_id=${encodeURIComponent(id)}`;
      return;
    }
    if (["copy", "delete", "enable", "disable", "run"].includes(action)) await mutateTemplate(item, action);
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
    onReady: async auth => {
      state.operatorName = String(auth?.user?.name || "");
      bindEvents();
      await loadTemplates();
    },
  });
})();
