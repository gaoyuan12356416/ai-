(function () {
  "use strict";

  const ui = window.XAutoPublish;
  const templateId = ui.pageIdFromQuery("id");
  document.title = `${templateId ? "编辑" : "创建"} X Post 自动发布模板 - AI自动后台`;
  const RESOURCE_TYPE_V2_OPTIONS = Object.freeze([
    { value: "0", label: "其他" },
    { value: "1", label: "翻译剧非首发" },
    { value: "2", label: "本土首发" },
    { value: "3", label: "本土对投" },
    { value: "4", label: "本土二轮采买" },
    { value: "5", label: "本土自制" },
    { value: "6", label: "翻译剧首发" },
    { value: "7", label: "首发本土动态漫" },
    { value: "8", label: "二轮本土动态漫" },
    { value: "9", label: "首发翻译动态漫" },
    { value: "10", label: "二轮翻译动态漫" },
    { value: "11", label: "翻译剧自制" },
    { value: "12", label: "漫剧自制" },
    { value: "13", label: "AI本土真人剧自制" },
    { value: "14", label: "AI本土真人剧首发" },
    { value: "15", label: "二轮本土AI真人剧" },
    { value: "16", label: "翻译AI真人剧首发" },
    { value: "17", label: "二轮翻译AI真人剧" },
    { value: "18", label: "AI本土解说剧自制" },
    { value: "19", label: "AI本土解说剧首发" },
    { value: "20", label: "AI本土解说剧二轮" },
    { value: "21", label: "AI翻译解说剧首发" },
    { value: "22", label: "AI翻译解说剧首发" },
    { value: "100", label: "小说" },
  ]);
  const RESOURCE_TYPE_V2_VALUES = new Set(RESOURCE_TYPE_V2_OPTIONS.map(item => item.value));
  const state = {
    accounts: [],
    selectedAccountIds: new Set(),
    selectedResourceTypes: new Set(),
    template: null,
    version: 0,
    busy: false,
    dirty: false,
  };

  function accountId(item) {
    return String(item && (item.id || item.account_id || item.source_account_id) || "").trim();
  }

  function accountName(item) {
    return String(item && (item.display_name || item.account_name || item.username || item.nickname) || `账号 ${accountId(item)}`);
  }

  function accountSubscription(item) {
    return String(item && (item.subscription_type || item.membership_type) || "unknown").trim().toLowerCase();
  }

  function accountEligible(item) {
    if (item && item.publish_eligible !== undefined) return ui.boolValue(item.publish_eligible);
    const status = String(item && item.status || "").toLowerCase();
    return status === "active" && ui.boolValue(item && item.publish_approved);
  }

  function statusText(item) {
    if (!item || !item.id) return "新模板";
    return ui.boolValue(item.enabled) || item.status === "enabled" ? "已启用" : "已停用";
  }

  function renderAccounts() {
    const list = ui.byId("accountList");
    ui.clear(list);
    const query = ui.byId("accountSearch").value.trim().toLowerCase();
    const visible = state.accounts.filter(item => {
      if (!query) return true;
      return `${accountId(item)} ${accountName(item)} ${accountSubscription(item)} ${item && item.status || ""}`.toLowerCase().includes(query);
    });
    if (!visible.length) {
      list.appendChild(ui.element("div", { className: "empty", text: "没有匹配的账号。" }));
      updateSummary();
      return;
    }
    visible.forEach(item => {
      const id = accountId(item);
      if (!id) return;
      const label = ui.element("label", { className: "account-option" });
      const eligible = accountEligible(item);
      const checkbox = ui.element("input", {
        type: "checkbox",
        attributes: { "aria-label": `选择 ${accountName(item)}` },
        dataset: { accountId: id, accountEligible: eligible ? "1" : "0" },
      });
      checkbox.checked = state.selectedAccountIds.has(id);
      checkbox.disabled = !eligible && !checkbox.checked;
      const details = ui.element("div");
      details.appendChild(ui.element("strong", { text: accountName(item) }));
      details.appendChild(ui.element("span", { className: "secondary mono", text: id }));
      const meta = ui.element("div");
      const subscription = accountSubscription(item);
      meta.appendChild(ui.element("span", {
        className: `language-chip${eligible ? "" : " badge warning"}`,
        text: eligible ? "可发布" : "当前不可发布",
      }));
      meta.appendChild(ui.element("span", { className: "secondary", text: `订阅 ${subscription}` }));
      label.append(checkbox, details, meta);
      list.appendChild(label);
    });
    updateSummary();
  }

  function selectedScheduleMode() {
    return ui.byId("scheduleModeRandom").checked ? "random" : "fixed";
  }

  function timeValues() {
    return Array.from(ui.byId("publishTimes").querySelectorAll("input[data-publish-time]"))
      .map(input => input.value.trim())
      .filter(Boolean);
  }

  function addTimeRow(value) {
    const container = ui.byId("publishTimes");
    const row = ui.element("div", { className: "time-row" });
    const input = ui.element("input", {
      type: "time",
      attributes: { step: "60", "data-publish-time": "1", "aria-label": "固定发布时间" },
    });
    input.value = /^\d{2}:\d{2}$/.test(String(value || "")) ? String(value) : "11:00";
    const remove = ui.element("button", { className: "button small", text: "删除", type: "button", dataset: { removeTime: "1" } });
    row.append(input, remove);
    container.appendChild(row);
    syncTimeRemoveButtons();
  }

  function syncTimeRemoveButtons() {
    const buttons = ui.byId("publishTimes").querySelectorAll("button[data-remove-time]");
    buttons.forEach(button => { button.disabled = buttons.length <= 1; });
  }

  function setTimeRows(values) {
    ui.clear(ui.byId("publishTimes"));
    const times = Array.isArray(values) && values.length ? values : ["11:00"];
    times.forEach(addTimeRow);
  }

  function setValue(id, value, fallback) {
    const node = ui.byId(id);
    if (!node) return;
    node.value = value == null || value === "" ? (fallback == null ? "" : fallback) : String(value);
  }

  function resourceTypeText(item) {
    return `${item.label}（${item.value}）`;
  }

  function setResourceTypeMenuOpen(open) {
    ui.byId("dramaResourceTypeMenu").classList.toggle("hidden", !open);
    ui.byId("dramaResourceTypes").setAttribute("aria-expanded", open ? "true" : "false");
  }

  function updateResourceTypeLabel() {
    const selected = RESOURCE_TYPE_V2_OPTIONS.filter(item => state.selectedResourceTypes.has(item.value));
    let text = "不限类型";
    if (selected.length === 1) text = resourceTypeText(selected[0]);
    else if (selected.length === 2) text = selected.map(resourceTypeText).join("、");
    else if (selected.length > 2) text = `已选择 ${selected.length} 种类型`;
    ui.setText(ui.byId("dramaResourceTypeLabel"), text, "不限类型");
  }

  function renderResourceTypes() {
    const options = ui.byId("dramaResourceTypeOptions");
    ui.clear(options);
    RESOURCE_TYPE_V2_OPTIONS.forEach(item => {
      const label = ui.element("label", { className: "multi-select-option" });
      const checkbox = ui.element("input", {
        type: "checkbox",
        attributes: { "aria-label": resourceTypeText(item) },
        dataset: { resourceType: item.value },
      });
      checkbox.checked = state.selectedResourceTypes.has(item.value);
      label.append(checkbox, ui.element("span", { text: resourceTypeText(item) }));
      options.appendChild(label);
    });
    updateResourceTypeLabel();
  }

  function setResourceTypes(values) {
    const normalized = Array.isArray(values)
      ? values.map(value => String(value).trim()).filter(value => RESOURCE_TYPE_V2_VALUES.has(value))
      : [];
    state.selectedResourceTypes = new Set(normalized);
    renderResourceTypes();
  }

  function ruleValue(rule, key) {
    const value = ui.objectValue(rule)[key];
    return value == null ? "" : value;
  }

  function hydrateTemplate(item) {
    state.template = ui.objectValue(item);
    const config = ui.objectValue(state.template.config);
    const configValue = (key, fallback) => state.template[key] !== undefined ? state.template[key] : config[key] !== undefined ? config[key] : fallback;
    state.version = Math.max(0, ui.numberValue(state.template.version || state.template.current_version, 0));
    setValue("templateName", state.template.name);
    setValue("templateLanguage", configValue("language", ""));
    setValue("platform", configValue("platform", 0), "0");
    setValue("metricWindowDays", configValue("metric_window_days", 7), "7");
    setValue("bodyTemplate", configValue("body_template", ""));
    setValue("dramaLaunchWindowDays", configValue("drama_launch_window_days", 0), "0");
    setValue("cooldownDays", configValue("cooldown_days", 0), "0");

    const drama = ui.objectValue(configValue("drama_rule", {}));
    const material = ui.objectValue(configValue("material_rule", {}));
    const resourceTypes = Array.isArray(drama.resource_type_v2) ? drama.resource_type_v2 : [];
    setResourceTypes(resourceTypes);
    setValue("dramaRoasMin", ruleValue(drama, "roas_min"));
    setValue("dramaRoasMax", ruleValue(drama, "roas_max"));
    setValue("dramaSpendMin", ruleValue(drama, "spend_min"));
    setValue("dramaSpendMax", ruleValue(drama, "spend_max"));
    setValue("dramaSortBy", drama.sort_by, "roas");
    setValue("dramaSortDirection", drama.sort_direction, "desc");
    setValue("materialDurationMin", ruleValue(material, "duration_min_seconds"));
    setValue("materialDurationMax", ruleValue(material, "duration_max_seconds"));
    setValue("materialRoasMin", ruleValue(material, "roas_min"));
    setValue("materialRoasMax", ruleValue(material, "roas_max"));
    setValue("materialSpendMin", ruleValue(material, "spend_min"));
    setValue("materialSpendMax", ruleValue(material, "spend_max"));
    setValue("materialSortBy", material.sort_by, "roas");
    setValue("materialSortDirection", material.sort_direction, "desc");

    const rawAccountIds = configValue("account_ids", []);
    const accountIds = Array.isArray(rawAccountIds) ? rawAccountIds : [];
    state.selectedAccountIds = new Set(accountIds.map(String));
    const schedule = ui.objectValue(configValue("schedule", {}));
    const randomMode = schedule.mode === "random";
    ui.byId("scheduleModeRandom").checked = randomMode;
    ui.byId("scheduleModeFixed").checked = !randomMode;
    setTimeRows(schedule.times);
    setValue("randomDailyCount", schedule.daily_count || schedule.random_daily_count, "1");
    renderScheduleMode();

    ui.setText(ui.byId("pageTitle"), `编辑 X Post 自动发布模板 · ${state.template.name || templateId}`, "");
    const badge = ui.byId("templateStatusBadge");
    ui.setText(badge, statusText(state.template), "");
    badge.className = `badge ${ui.boolValue(state.template.enabled) || state.template.status === "enabled" ? "success" : "warning"}`;
    renderAccounts();
    updateSummary();
    state.dirty = false;
  }

  function renderScheduleMode() {
    const random = selectedScheduleMode() === "random";
    ui.byId("fixedScheduleFields").classList.toggle("hidden", random);
    ui.byId("randomScheduleFields").classList.toggle("hidden", !random);
    updateSummary();
  }

  function sortSummary(prefix) {
    const metric = ui.byId(`${prefix}SortBy`).value === "spend" ? "消耗" : "D0 ROAS";
    const direction = ui.byId(`${prefix}SortDirection`).value === "asc" ? "正序" : "倒序";
    return `${metric} / ${direction}`;
  }

  function updateSummary() {
    const count = state.selectedAccountIds.size;
    ui.setText(ui.byId("selectedAccountCount"), `已选 ${count} 个`, "");
    ui.setText(ui.byId("summaryVersion"), state.version ? `v${state.version}` : "新模板", "");
    ui.setText(ui.byId("summaryLanguage"), ui.byId("templateLanguage").value.trim() || "未设置", "");
    ui.setText(ui.byId("summaryAccounts"), `${count} 个`, "");
    ui.setText(ui.byId("summaryMetric"), `platform=${ui.byId("platform").value || "0"} / ${ui.byId("metricWindowDays").value || "7"} 天`, "");
    ui.setText(ui.byId("summaryDramaSort"), sortSummary("drama"), "");
    ui.setText(ui.byId("summaryMaterialSort"), sortSummary("material"), "");
    if (selectedScheduleMode() === "random") {
      ui.setText(ui.byId("summarySchedule"), `每天随机 ${ui.byId("randomDailyCount").value || "1"} 次`, "");
    } else {
      const times = timeValues();
      ui.setText(ui.byId("summarySchedule"), times.length ? `每天 ${times.join("、")}` : "未设置", "");
    }
    const bodyTemplate = ui.byId("bodyTemplate").value;
    ui.setText(ui.byId("bodyTemplateCount"), `${bodyTemplate.length} / 2000`, "");
  }

  function parseResourceTypes() {
    return RESOURCE_TYPE_V2_OPTIONS
      .filter(item => state.selectedResourceTypes.has(item.value))
      .map(item => item.value);
  }

  function decimalOrNull(id) {
    const raw = ui.byId(id).value.trim();
    return raw === "" ? null : raw;
  }

  function integerValue(id, fallback) {
    const raw = ui.byId(id).value.trim();
    if (raw === "" && fallback !== undefined) return fallback;
    return Number(raw);
  }

  function validateRange(minId, maxId, label) {
    const min = decimalOrNull(minId);
    const max = decimalOrNull(maxId);
    if (min != null && Number(min) < 0 || max != null && Number(max) < 0) throw new Error(`${label}不能小于 0。`);
    if (min != null && max != null && Number(min) > Number(max)) throw new Error(`${label}最小值不能大于最大值。`);
  }

  function buildPayload() {
    const name = ui.byId("templateName").value.trim();
    if (!name) throw new Error("请填写模板名称。");
    const language = ui.byId("templateLanguage").value.trim().toLowerCase();
    if (!language) throw new Error("请填写模板剧语言。");
    if (language.length < 2 || language.length > 32 || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(language)) throw new Error("模板剧语言必须为 2–32 位语言代码，例如 en 或 en-us。");
    if (!state.selectedAccountIds.size) throw new Error("请至少选择一个 X 账号。");
    if (state.selectedAccountIds.size > 100) throw new Error("一个模板最多选择 100 个 X 账号。");
    const bodyTemplate = ui.byId("bodyTemplate").value.replace(/\r\n?/g, "\n").trim();
    if (!bodyTemplate) throw new Error("请填写 X Post 正文模板。");
    if (bodyTemplate.length > 2000) throw new Error("X Post 正文模板不能超过 2000 个字符。");
    const macros = Array.from(bodyTemplate.matchAll(/\{\{([a-z_]+)\}\}/g), match => match[1]);
    const unmatched = bodyTemplate.replace(/\{\{([a-z_]+)\}\}/g, "");
    if (unmatched.includes("{{") || unmatched.includes("}}")) throw new Error("X Post 正文模板包含不完整或格式无效的宏。");
    const allowedMacros = new Set(["drama_name", "desc", "url"]);
    const unknownMacros = Array.from(new Set(macros.filter(macro => !allowedMacros.has(macro))));
    if (unknownMacros.length) throw new Error(`X Post 正文模板包含不支持的宏：${unknownMacros.join("、")}。`);
    for (const requiredMacro of ["drama_name", "desc"]) {
      if (macros.filter(macro => macro === requiredMacro).length !== 1) throw new Error(`X Post 正文模板必须且只能包含一次 {{${requiredMacro}}}。`);
    }
    if (macros.filter(macro => macro === "url").length > 1) throw new Error("X Post 正文模板中的 {{url}} 最多出现一次。");
    const metricWindowDays = integerValue("metricWindowDays", 7);
    if (!Number.isInteger(metricWindowDays) || metricWindowDays < 1 || metricWindowDays > 30) throw new Error("指标统计窗口必须为 1–30 天。");
    const launchWindow = integerValue("dramaLaunchWindowDays", 0);
    const cooldown = integerValue("cooldownDays", 0);
    if (!Number.isInteger(launchWindow) || launchWindow < 0) throw new Error("剧上线窗口必须为大于等于 0 的整数。");
    if (!Number.isInteger(cooldown) || cooldown < 0) throw new Error("同剧冷却窗口必须为大于等于 0 的整数。");

    validateRange("dramaRoasMin", "dramaRoasMax", "剧 D0 ROAS");
    validateRange("dramaSpendMin", "dramaSpendMax", "剧消耗");
    validateRange("materialRoasMin", "materialRoasMax", "素材 D0 ROAS");
    validateRange("materialSpendMin", "materialSpendMax", "素材消耗");
    validateRange("materialDurationMin", "materialDurationMax", "素材时长");
    const durationMin = integerValue("materialDurationMin");
    const durationMax = integerValue("materialDurationMax");
    if (!Number.isInteger(durationMin) || durationMin < 1 || durationMin > 600) throw new Error("素材最小时长必须为 1–600 秒的整数。");
    if (!Number.isInteger(durationMax) || durationMax < 1 || durationMax > 600) throw new Error("素材最长时长必须为 1–600 秒的整数。");
    if (durationMin > durationMax) throw new Error("素材时长最小值不能大于最大值。");

    const mode = selectedScheduleMode();
    let schedule;
    if (mode === "random") {
      const dailyCount = integerValue("randomDailyCount");
      if (!Number.isInteger(dailyCount) || dailyCount < 1 || dailyCount > 24) throw new Error("每天随机发布次数必须为 1–24。");
      schedule = { mode: "random", daily_count: dailyCount };
    } else {
      const times = Array.from(new Set(timeValues())).sort();
      if (!times.length) throw new Error("请至少设置一个固定发布时间。");
      if (times.some(value => !/^([01]\d|2[0-3]):[0-5]\d$/.test(value))) throw new Error("固定发布时间格式无效。");
      schedule = { mode: "fixed", times };
    }

    const payload = {
      name,
      language,
      account_ids: Array.from(state.selectedAccountIds),
      body_template: bodyTemplate,
      metric_window_days: metricWindowDays,
      drama_launch_window_days: launchWindow,
      cooldown_days: cooldown,
      platform: integerValue("platform", 0),
      drama_rule: {
        resource_type_v2: parseResourceTypes(),
        spend_min: decimalOrNull("dramaSpendMin"),
        spend_max: decimalOrNull("dramaSpendMax"),
        roas_min: decimalOrNull("dramaRoasMin"),
        roas_max: decimalOrNull("dramaRoasMax"),
        sort_by: ui.byId("dramaSortBy").value,
        sort_direction: ui.byId("dramaSortDirection").value,
      },
      material_rule: {
        duration_min_seconds: durationMin,
        duration_max_seconds: durationMax,
        spend_min: decimalOrNull("materialSpendMin"),
        spend_max: decimalOrNull("materialSpendMax"),
        roas_min: decimalOrNull("materialRoasMin"),
        roas_max: decimalOrNull("materialRoasMax"),
        sort_by: ui.byId("materialSortBy").value,
        sort_direction: ui.byId("materialSortDirection").value,
      },
      schedule,
    };
    if (templateId) payload.expected_version = state.version;
    return payload;
  }

  async function loadAccounts() {
    const payload = await ui.api(`${ui.API_BASE}/accounts`);
    state.accounts = ui.readItems(payload, ["accounts", "items"]);
  }

  async function loadTemplate() {
    if (!templateId) return null;
    const payload = await ui.api(`${ui.API_BASE}/templates/${templateId}`);
    return ui.readItem(payload, ["template", "item"]);
  }

  async function saveTemplate(event) {
    event.preventDefault();
    if (state.busy) return;
    let payload;
    try {
      payload = buildPayload();
    } catch (error) {
      ui.setText(ui.byId("formStatus"), error.message, "");
      ui.byId("formStatus").className = "status-line error";
      ui.showToast(error.message, true);
      return;
    }
    state.busy = true;
    ui.byId("saveTemplate").disabled = true;
    ui.setText(ui.byId("formStatus"), "正在保存模板…", "");
    ui.byId("formStatus").className = "status-line";
    try {
      const path = templateId ? `${ui.API_BASE}/templates/${templateId}` : `${ui.API_BASE}/templates`;
      const response = await ui.api(path, { method: "POST", body: JSON.stringify(payload) });
      const saved = ui.readItem(response, ["template", "item"]);
      const savedId = ui.positiveId(saved.id || saved.template_id || response.template_id || templateId);
      state.dirty = false;
      ui.setText(ui.byId("formStatus"), "模板已保存。", "");
      ui.byId("formStatus").className = "status-line success";
      ui.showToast("模板已保存；新模板默认保持关闭。", false);
      location.href = savedId ? `/x-auto-publish-template.html?id=${encodeURIComponent(savedId)}` : "/x-auto-publish-templates.html";
    } catch (error) {
      ui.setText(ui.byId("formStatus"), error.message || "模板保存失败。", "");
      ui.byId("formStatus").className = "status-line error";
      ui.showToast(error.message || "模板保存失败", true);
    } finally {
      state.busy = false;
      ui.byId("saveTemplate").disabled = false;
    }
  }

  function bindEvents() {
    ui.byId("templateForm").addEventListener("submit", event => void saveTemplate(event));
    ui.byId("dramaResourceTypes").addEventListener("click", () => {
      const expanded = ui.byId("dramaResourceTypes").getAttribute("aria-expanded") === "true";
      setResourceTypeMenuOpen(!expanded);
    });
    ui.byId("dramaResourceTypeOptions").addEventListener("change", event => {
      const checkbox = event.target.closest("input[data-resource-type]");
      if (!checkbox) return;
      if (checkbox.checked) state.selectedResourceTypes.add(checkbox.dataset.resourceType);
      else state.selectedResourceTypes.delete(checkbox.dataset.resourceType);
      state.dirty = true;
      updateResourceTypeLabel();
    });
    ui.byId("clearDramaResourceTypes").addEventListener("click", () => {
      state.selectedResourceTypes.clear();
      ui.byId("dramaResourceTypeOptions").querySelectorAll("input[data-resource-type]").forEach(checkbox => {
        checkbox.checked = false;
      });
      state.dirty = true;
      updateResourceTypeLabel();
    });
    document.addEventListener("click", event => {
      if (!event.target.closest("#dramaResourceTypePicker")) setResourceTypeMenuOpen(false);
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") setResourceTypeMenuOpen(false);
    });
    ui.byId("accountSearch").addEventListener("input", renderAccounts);
    ui.byId("accountList").addEventListener("change", event => {
      const checkbox = event.target.closest("input[data-account-id]");
      if (!checkbox) return;
      if (checkbox.checked) state.selectedAccountIds.add(checkbox.dataset.accountId);
      else state.selectedAccountIds.delete(checkbox.dataset.accountId);
      state.dirty = true;
      updateSummary();
    });
    ui.byId("selectVisibleAccounts").addEventListener("click", () => {
      ui.byId("accountList").querySelectorAll("input[data-account-id]").forEach(checkbox => {
        if (checkbox.dataset.accountEligible !== "1") return;
        checkbox.checked = true;
        state.selectedAccountIds.add(checkbox.dataset.accountId);
      });
      state.dirty = true;
      updateSummary();
    });
    ui.byId("clearAccounts").addEventListener("click", () => {
      state.selectedAccountIds.clear();
      state.dirty = true;
      renderAccounts();
    });
    ui.byId("addPublishTime").addEventListener("click", () => {
      addTimeRow("11:00");
      state.dirty = true;
      updateSummary();
    });
    ui.byId("publishTimes").addEventListener("click", event => {
      const button = event.target.closest("button[data-remove-time]");
      if (!button || button.disabled) return;
      button.closest(".time-row")?.remove();
      syncTimeRemoveButtons();
      state.dirty = true;
      updateSummary();
    });
    ui.byId("scheduleModeFixed").addEventListener("change", renderScheduleMode);
    ui.byId("scheduleModeRandom").addEventListener("change", renderScheduleMode);
    ui.byId("templateForm").addEventListener("input", () => {
      state.dirty = true;
      updateSummary();
    });
    ui.byId("templateForm").addEventListener("change", () => {
      state.dirty = true;
      updateSummary();
    });
    window.addEventListener("beforeunload", event => {
      if (!state.dirty || state.busy) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  void ui.initShell({
    activeKey: "xAutoPublishTemplates",
    onReady: async () => {
      bindEvents();
      setTimeRows(["11:00"]);
      setResourceTypes([]);
      try {
        const [accountsResult, templateResult] = await Promise.all([loadAccounts(), loadTemplate()]);
        void accountsResult;
        if (templateId) {
          if (!templateResult || !templateResult.id && !templateResult.template_id) throw new Error("模板不存在或不可访问。");
          hydrateTemplate(templateResult);
        } else {
          renderAccounts();
          renderScheduleMode();
          updateSummary();
          state.dirty = false;
        }
      } catch (error) {
        ui.setText(ui.byId("formStatus"), error.message || "页面数据加载失败。", "");
        ui.byId("formStatus").className = "status-line error";
        ui.byId("saveTemplate").disabled = true;
        ui.showToast(error.message || "页面数据加载失败", true);
      }
    },
  });
})();
