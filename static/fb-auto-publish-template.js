(() => {
  "use strict";

  const ui = window.FBAutoPublishUI;
  const templateId = ui.positiveId(new URLSearchParams(window.location.search).get("id"));
  ui.byId("pageTitle").textContent = `${templateId ? "编辑" : "创建"} FB Page 自动发布模板`;
  document.title = ui.byId("pageTitle").textContent + " - AI自动后台";
  const state = {
    groups: [],
    template: null,
    busy: false,
    fixedStaggerMinutes: 0,
    groupsLoaded: false,
    groupsLoading: false,
    templateReady: false,
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
    const saved = state.template?.config || {};
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
      page_daily_limits: saved.page_daily_limits,
      default_daily_count: saved.default_daily_count,
      feedback_selection: saved.feedback_selection,
      message_template: ui.byId("message").value,
      video_template: ui.byId("videoTemplate").value,
      material_data_source: Number(ui.byId("dataSource").value),
      material_products: window.MaterialProductPicker.read(),
      metric_window_days: Number(ui.byId("metricDays").value),
      drama_launch_window_days: window.normalizeDramaLaunchWindow(ui.byId("launchDays").value),
      cooldown_days: Number(ui.byId("cooldown").value),
      drama_cooldown_hours: Number(ui.byId("dramaCooldown").value),
      stagger_minutes: schedule.mode === "fixed" ? Number(ui.byId("staggerMinutes").value) : 0,
      drama_rule: {
        ...range("d"),
        resource_type_v2: saved.drama_rule?.resource_type_v2 || [],
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
    if (!state.groupsLoaded) return;
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
    state.groupsLoaded = true;
    const summary = data.summary || {};
    ui.byId("groupCount").textContent = summary.total_groups ?? 0;
    ui.byId("pageCount").textContent = summary.total_pages ?? 0;
    ui.byId("publishableCount").textContent = summary.publishable_pages ?? 0;
    ui.byId("missingCount").textContent = summary.missing_token_pages ?? 0;
    renderGroups();
    estimate();
  }

  async function loadGroupOptions() {
    if (state.groupsLoading) return;
    state.groupsLoading = true;
    ui.byId("poolList").textContent = "正在加载 Page 池…";
    try {
      await loadGroups();
    } catch (error) {
      ui.byId("poolList").textContent = error.message || "Page 池读取失败，已保存的选择仍保留。";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "button small";
      retry.textContent = "重新加载 Page 池";
      retry.addEventListener("click", () => void loadGroupOptions());
      ui.byId("poolList").appendChild(retry);
    } finally {
      state.groupsLoading = false;
      ui.byId("saveTemplate").disabled = !state.templateReady || !state.groupsLoaded || state.busy;
    }
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
    if (!Array.from(ui.byId("videoTemplate").options).some(option => option.value === config.video_template)) {
      throw new Error("该模板缺少受支持的视频制作模板，无法编辑。");
    }
    ui.byId("pageTitle").textContent = "编辑 FB Page 自动发布模板";
    document.title = "编辑 FB Page 自动发布模板 - AI自动后台";
    ui.byId("templateStatusBadge").textContent = (item.status === "enabled" ? "已启用" : "已停用") + " · v" + ui.templateVersion(item);
    ui.byId("templateStatusBadge").className = "badge " + (item.status === "enabled" ? "success" : "warning");
    ui.byId("name").value = config.name || item.name || "";
    ui.byId("message").value = config.message_template || "";
    ui.byId("videoTemplate").value = config.video_template;
    ui.byId("dataSource").value = Number(config.material_data_source || 6);
    window.MaterialProductPicker.render(config);
    ui.byId("metricDays").value = Number(config.metric_window_days || 7);
    ui.byId("launchDays").value = config.drama_launch_window_days ?? 0;
    ui.byId("cooldown").value = Number(config.cooldown_days || 0);
    ui.byId("dramaCooldown").value = Number(config.drama_cooldown_hours || 0);
    ui.byId("staggerMinutes").value = Number(config.stagger_minutes || 0);
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
    if (!state.groupsLoaded) {
      const count = state.template?.config?.group_ids?.length || 0;
      ui.byId("capacityEstimate").textContent = `已选 ${count} 个 Page 池，正在读取池内账号与发布容量…`;
      return;
    }
    const selected = Array.from(document.querySelectorAll('input[name="group"]:checked'))
      .map(input => state.groups.find(group => String(group.group_id) === input.value))
      .filter(Boolean);
    const pages = selected.reduce((sum, group) => sum + Number(group.publishable_pages || 0), 0);
    const frequency = ui.byId("scheduleMode").value === "fixed"
      ? ui.byId("fixedTimes").value.split(",").map(value => value.trim()).filter(Boolean).length
      : Number(ui.byId("randomCount").value || 0);
    const daily = pages * frequency;
    ui.byId("capacityEstimate").textContent = "每日发布上限约 " + daily + " 条（" + pages + " 个可发 Page × " + frequency + " 次）。授权、候选与冷却会影响实际数量；启用时由后台复核。";
  }

  function renderTargetDayPlans(plans) {
    const container = ui.byId("targetDayPlan");
    const days = Array.isArray(plans) ? plans : [];
    container.replaceChildren();
    container.classList.toggle("hidden", !days.length);
    if (!days.length) return;
    container.innerHTML = '<div class="target-plan-heading"><strong>已生成排期</strong><span class="helper">北京时间 · 修改表单不会改变下方已生成的时间</span></div>';
    days.forEach((day, index) => {
      const targets = Array.isArray(day.targets) ? day.targets : [];
      const timesFor = target => Array.isArray(target.times) ? target.times : [];
      const total = targets.reduce((sum, target) => sum + timesFor(target).length, 0);
      const details = document.createElement("details");
      details.className = "target-plan";
      details.innerHTML = '<summary><span class="target-plan-date">' + ui.escapeHtml(day.local_date) + '</span>'
        + '<span class="target-plan-count">' + targets.length + ' 个 Page · ' + total + ' 次发布</span>'
        + '<span class="target-plan-toggle" aria-hidden="true"></span></summary>'
        + '<div class="target-plan-body"><label for="targetPlanSearch' + index + '">查找 Page ID</label>'
        + '<input id="targetPlanSearch' + index + '" type="search" placeholder="输入完整或部分 Page ID" autocomplete="off" />'
        + '<p class="helper target-plan-matches" role="status" aria-live="polite"></p>'
        + '<div class="target-plan-scroll" tabindex="0" role="region" aria-label="' + ui.escapeHtml(day.local_date) + ' Page 发布时间">'
        + '<table><thead><tr><th scope="col">Page ID</th><th scope="col">发布时间（北京）</th></tr></thead><tbody></tbody></table></div></div>';
      const search = details.querySelector("input");
      const renderRows = () => {
        const query = search.value.trim();
        const filtered = targets.filter(target => String(target.target_id).includes(query));
        details.querySelector(".target-plan-matches").textContent = query
          ? "找到 " + filtered.length + " / " + targets.length + " 个 Page"
          : "共 " + targets.length + " 个 Page，滚动查看全部时间";
        details.querySelector("tbody").innerHTML = filtered.map(target => '<tr><td class="target-plan-id">' + ui.escapeHtml(target.target_id)
          + '</td><td><div class="target-plan-times">' + (timesFor(target).map(time => '<span>' + ui.escapeHtml(time) + '</span>').join("") || '<span>暂无时间</span>')
          + '</div></td></tr>').join("") || '<tr><td colspan="2" class="empty">' + (query ? "没有匹配的 Page" : "暂无 Page 排期") + '</td></tr>';
      };
      let rendered = false;
      details.addEventListener("toggle", () => {
        if (details.open && !rendered) { renderRows(); rendered = true; }
      });
      search.addEventListener("input", renderRows);
      container.appendChild(details);
    });
  }

  function toggleSchedule() {
    const fixed = ui.byId("scheduleMode").value === "fixed";
    ui.byId("fixedField").classList.toggle("hidden", !fixed);
    ui.byId("randomField").classList.toggle("hidden", fixed);
    const stagger = ui.byId("staggerMinutes");
    if (!fixed && !stagger.disabled) {
      state.fixedStaggerMinutes = Number(stagger.value || 0);
      stagger.value = 0;
    } else if (fixed && stagger.disabled) {
      stagger.value = state.fixedStaggerMinutes;
    }
    stagger.disabled = !fixed;
    ui.byId("staggerField").classList.toggle("hidden", !fixed);
    estimate();
  }

  async function saveTemplate(event) {
    event.preventDefault();
    if (!state.templateReady || !state.groupsLoaded || state.busy) return;
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
      window.MaterialProductPicker.render();
      bindEvents();
      ui.byId("retryEditorLoad").addEventListener("click", () => window.location.reload());
      const templateRequest = loadTemplate();
      void loadGroupOptions();
      try {
        if (templateId) {
          const item = await templateRequest;
          if (!ui.positiveId(item && (item.id || item.template_id))) throw new Error("模板不存在或不可访问。");
          hydrateTemplate(item);
          renderTargetDayPlans(item.target_daily_plans);
        } else {
          toggleSchedule();
        }
        state.templateReady = true;
        ui.byId("editorLoading").classList.add("hidden");
        ui.byId("editorContent").classList.remove("hidden");
        ui.byId("saveTemplate").disabled = !state.groupsLoaded;
      } catch (error) {
        ui.byId("editorLoadingMessage").textContent = error.message || "模板配置读取失败，请重试。";
        ui.byId("retryEditorLoad").classList.remove("hidden");
        ui.byId("saveTemplate").disabled = true;
        ui.byId("formMessage").textContent = error.message || "页面数据加载失败";
        ui.byId("formMessage").className = "status-line error";
        throw error;
      }
    },
  });
})();
