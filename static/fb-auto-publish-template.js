(() => {
  "use strict";

  const ui = window.FBAutoPublishUI;
  const templateId = ui.positiveId(new URLSearchParams(window.location.search).get("id"));
  const state = {
    groups: [],
    template: null,
    busy: false,
  };

  function range(prefix) {
    return {
      spend_min: ui.byId(prefix + "SpendMin").value || null,
      spend_max: ui.byId(prefix + "SpendMax").value || null,
      roas_min: ui.byId(prefix + "RoasMin").value || null,
      roas_max: ui.byId(prefix + "RoasMax").value || null,
      sort_by: ui.byId("sortBy").value,
      sort_direction: ui.byId("sortDirection").value,
    };
  }

  function buildPayload() {
    const schedule = ui.byId("scheduleMode").value === "fixed"
      ? {
          mode: "fixed",
          times: ui.byId("fixedTimes").value.split(",").map(value => value.trim()).filter(Boolean),
        }
      : {
          mode: "random",
          daily_count: Number(ui.byId("randomCount").value),
          start: ui.byId("randomStart").value,
          end: ui.byId("randomEnd").value,
        };
    return {
      name: ui.byId("name").value,
      group_ids: Array.from(document.querySelectorAll('input[name="group"]:checked')).map(input => input.value),
      language: ui.byId("language").value,
      message_template: ui.byId("message").value,
      video_template: ui.byId("videoTemplate").value,
      material_data_source: Number(ui.byId("dataSource").value),
      metric_window_days: Number(ui.byId("metricDays").value),
      drama_launch_window_days: Number(ui.byId("launchDays").value),
      cooldown_days: Number(ui.byId("cooldown").value),
      drama_rule: {
        ...range("d"),
        resource_type_v2: [],
      },
      material_rule: {
        ...range("m"),
        duration_min_seconds: Number(ui.byId("durationMin").value),
        duration_max_seconds: Number(ui.byId("durationMax").value),
      },
      schedule,
    };
  }

  function renderGroups() {
    const selected = new Set(((state.template && state.template.config || {}).group_ids || []).map(String));
    ui.byId("poolList").innerHTML = state.groups.map(group => [
      '<label class="pool">',
      '<input type="checkbox" name="group" value="', ui.escapeHtml(group.group_id), '"', selected.has(String(group.group_id)) ? " checked" : "", " />",
      "<span><strong>", ui.escapeHtml(group.name || ("池 " + group.group_id)), "</strong><br><small>",
      ui.escapeHtml(group.group_label), " · ", ui.escapeHtml(group.product), " (app ", ui.escapeHtml(group.app_id), ")</small></span>",
      "<small>总 ", Number(group.total_pages || 0), " / 可发 ", Number(group.publishable_pages || 0), " / 缺 ", Number(group.missing_token_pages || 0), "</small>",
      "</label>",
    ].join("")).join("") || '<div class="empty">无可见 Page 池</div>';
  }

  async function loadGroups() {
    const data = await ui.api(ui.API_BASE + "/groups");
    state.groups = Array.isArray(data.items) ? data.items : [];
    const summary = data.summary || {};
    ui.byId("groupCount").textContent = summary.total_groups ?? 0;
    ui.byId("pageCount").textContent = summary.total_pages ?? 0;
    ui.byId("publishableCount").textContent = summary.publishable_pages ?? 0;
    ui.byId("missingCount").textContent = summary.missing_token_pages ?? 0;
    renderGroups();
  }

  async function loadTemplate() {
    if (!templateId) return null;
    const data = await ui.api(ui.API_BASE + "/templates/" + templateId);
    state.template = ui.readItem(data);
    return state.template;
  }

  function setRange(prefix, rule) {
    const safeRule = rule || {};
    ui.byId(prefix + "SpendMin").value = safeRule.spend_min ?? "";
    ui.byId(prefix + "SpendMax").value = safeRule.spend_max ?? "";
    ui.byId(prefix + "RoasMin").value = safeRule.roas_min ?? "";
    ui.byId(prefix + "RoasMax").value = safeRule.roas_max ?? "";
  }

  function hydrateTemplate(item) {
    const config = item.config || {};
    if (config.video_template !== "random_overlay") throw new Error("该模板缺少受支持的视频制作模板，无法编辑。");
    ui.byId("pageTitle").textContent = "编辑 FB Page 自动发布模板";
    document.title = "编辑 FB Page 自动发布模板 - AI自动后台";
    ui.byId("templateStatusBadge").textContent = (item.status === "enabled" ? "已启用" : "已停用") + " · v" + ui.templateVersion(item);
    ui.byId("templateStatusBadge").className = "badge " + (item.status === "enabled" ? "success" : "warning");
    ui.byId("name").value = config.name || item.name || "";
    ui.byId("language").value = config.language || "en";
    ui.byId("message").value = config.message_template || "";
    ui.byId("videoTemplate").value = config.video_template;
    ui.byId("dataSource").value = Number(config.material_data_source || 6);
    ui.byId("metricDays").value = Number(config.metric_window_days || 7);
    ui.byId("launchDays").value = Number(config.drama_launch_window_days || 0);
    ui.byId("cooldown").value = Number(config.cooldown_days || 0);
    setRange("d", config.drama_rule);
    setRange("m", config.material_rule);
    const materialRule = config.material_rule || {};
    ui.byId("durationMin").value = Number(materialRule.duration_min_seconds ?? 1);
    ui.byId("durationMax").value = Number(materialRule.duration_max_seconds ?? 600);
    ui.byId("sortBy").value = materialRule.sort_by || "roas";
    ui.byId("sortDirection").value = materialRule.sort_direction || "desc";
    const schedule = config.schedule || {};
    ui.byId("scheduleMode").value = schedule.mode || "fixed";
    ui.byId("fixedTimes").value = (schedule.times || []).join(",") || "10:30";
    ui.byId("randomCount").value = Number(schedule.daily_count || 1);
    ui.byId("randomStart").value = schedule.start || "08:00";
    ui.byId("randomEnd").value = schedule.end || "23:00";
    renderGroups();
    toggleSchedule();
    estimate();
  }

  function estimate() {
    const selected = Array.from(document.querySelectorAll('input[name="group"]:checked'))
      .map(input => state.groups.find(group => String(group.group_id) === input.value))
      .filter(Boolean);
    const pages = selected.reduce((sum, group) => sum + Number(group.publishable_pages || 0), 0);
    const frequency = ui.byId("scheduleMode").value === "fixed"
      ? ui.byId("fixedTimes").value.split(",").map(value => value.trim()).filter(Boolean).length
      : Number(ui.byId("randomCount").value || 0);
    ui.byId("capacityEstimate").textContent = "预估：单时隙 " + pages + " 个 GPU 任务；每日 " + pages + " 个 Page × " + frequency + " 次 = " + (pages * frequency) + " 个 GPU 任务和 Graph 发布。最终以启用门禁去重统计为准。";
  }

  function toggleSchedule() {
    const fixed = ui.byId("scheduleMode").value === "fixed";
    ui.byId("fixedField").classList.toggle("hidden", !fixed);
    ui.byId("randomField").classList.toggle("hidden", fixed);
    estimate();
  }

  async function saveTemplate(event) {
    event.preventDefault();
    if (state.busy) return;
    state.busy = true;
    ui.byId("saveTemplate").disabled = true;
    ui.byId("formMessage").textContent = "正在保存…";
    ui.byId("formMessage").className = "status-line";
    try {
      const body = buildPayload();
      if (templateId) body.expected_version = ui.templateVersion(state.template);
      await ui.api(templateId ? ui.API_BASE + "/templates/" + templateId : ui.API_BASE + "/templates", {
        method: "POST",
        body: JSON.stringify(body),
      });
      ui.byId("formMessage").textContent = "保存成功，正在返回模板列表…";
      ui.byId("formMessage").className = "status-line success";
      window.location.href = "/fb-auto-publish-templates.html?v=20260820-list-only-v2";
    } catch (error) {
      ui.byId("formMessage").textContent = error.message || "模板保存失败";
      ui.byId("formMessage").className = "status-line error";
      ui.showToast(error.message || "模板保存失败", true);
    } finally {
      state.busy = false;
      ui.byId("saveTemplate").disabled = false;
    }
  }

  function bindEvents() {
    ui.byId("templateForm").addEventListener("submit", event => void saveTemplate(event));
    ui.byId("scheduleMode").addEventListener("change", toggleSchedule);
    ui.byId("fixedTimes").addEventListener("input", estimate);
    ui.byId("randomCount").addEventListener("input", estimate);
    ui.byId("poolList").addEventListener("change", estimate);
    ui.byId("resetForm").addEventListener("click", () => window.location.reload());
  }

  void ui.boot({
    onReady: async () => {
      bindEvents();
      try {
        await loadGroups();
        if (templateId) {
          const item = await loadTemplate();
          if (!ui.positiveId(item && (item.id || item.template_id))) throw new Error("模板不存在或不可访问。");
          hydrateTemplate(item);
        } else {
          toggleSchedule();
        }
      } catch (error) {
        ui.byId("saveTemplate").disabled = true;
        ui.byId("formMessage").textContent = error.message || "页面数据加载失败";
        ui.byId("formMessage").className = "status-line error";
        throw error;
      }
    },
  });
})();
