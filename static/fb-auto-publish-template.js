(() => {
  "use strict";

  const ui = window.FBAutoPublishUI;
  const templateId = ui.positiveId(new URLSearchParams(window.location.search).get("id"));
  const state = {
    groups: [],
    template: null,
    busy: false,
    pageLimits: [],
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
      message_template: ui.byId("message").value,
      video_template: ui.byId("videoTemplate").value,
      material_data_source: Number(ui.byId("dataSource").value),
      metric_window_days: Number(ui.byId("metricDays").value),
      drama_launch_window_days: Number(ui.byId("launchDays").value),
      cooldown_days: Number(ui.byId("cooldown").value),
      drama_cooldown_hours: Number(ui.byId("dramaCooldown").value),
      stagger_minutes: Number(ui.byId("staggerMinutes").value),
      ...(ui.byId("pageLimitsEnabled").checked ? {
        page_daily_limits: state.pageLimits.map(row => ({...row})),
        default_daily_count: Number(ui.byId("defaultDailyCount").value),
      } : {}),
      feedback_selection: {
        enabled: ui.byId("feedbackEnabled").checked,
        rollout_percent: Number(ui.byId("feedbackRollout").value),
        exploration_percent: Number(ui.byId("feedbackExploration").value),
      },
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
    ui.byId("message").value = config.message_template || "";
    ui.byId("videoTemplate").value = config.video_template;
    ui.byId("dataSource").value = Number(config.material_data_source || 6);
    ui.byId("metricDays").value = Number(config.metric_window_days || 7);
    ui.byId("launchDays").value = Number(config.drama_launch_window_days || 0);
    ui.byId("cooldown").value = Number(config.cooldown_days || 0);
    ui.byId("dramaCooldown").value = Number(config.drama_cooldown_hours || 0);
    ui.byId("staggerMinutes").value = Number(config.stagger_minutes || 0);
    ui.byId("pageLimitsEnabled").checked = Array.isArray(config.page_daily_limits) || config.default_daily_count !== undefined;
    ui.byId("defaultDailyCount").value = config.default_daily_count ?? 0;
    state.pageLimits = (config.page_daily_limits || []).map(row => ({...row}));
    const feedback = config.feedback_selection || {};
    ui.byId("feedbackEnabled").checked = feedback.enabled === true;
    ui.byId("feedbackRollout").value = feedback.rollout_percent ?? 50;
    ui.byId("feedbackExploration").value = feedback.exploration_percent ?? 20;
    renderPageLimits();
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
    const custom = ui.byId("pageLimitsEnabled").checked;
    const configured = state.pageLimits.reduce((sum,row) => sum + Math.min(frequency, row.daily_count), 0);
    const daily = custom ? configured + Math.max(0, pages-state.pageLimits.length) * Math.min(frequency, Number(ui.byId("defaultDailyCount").value || 0)) : pages * frequency;
    ui.byId("capacityEstimate").textContent = "每日发布上限约 " + daily + " 条" + (custom ? "（按 Page 频次）" : "（" + pages + " 个可发 Page × " + frequency + " 次）") + "。授权、候选与冷却会影响实际数量；启用时由后台复核。";
  }

  function renderPageLimits() {
    ui.byId("pageLimitsPanel").classList.toggle("hidden", !ui.byId("pageLimitsEnabled").checked);
    const query = ui.byId("pageLimitSearch").value.trim().toLowerCase();
    ui.byId("pageLimitsBody").innerHTML = state.pageLimits.map((row,index) => {
      if (query && ![row.page_id,row.name,row.tier].join(" ").toLowerCase().includes(query)) return "";
      return '<tr><td>' + ui.escapeHtml(row.tier === "hold" ? "暂停" : row.tier || "—") + '</td><td>' + ui.escapeHtml(row.name || row.page_id) + '<br><small>' + ui.escapeHtml(row.page_id) + '</small></td><td><input style="width:85px" type="number" min="0" max="24" aria-label="' + ui.escapeHtml(row.page_id + "每日次数") + '" data-page-limit="' + index + '" value="' + Number(row.daily_count) + '" /></td></tr>';
    }).join("") || '<tr><td colspan="3">没有匹配的 Page</td></tr>';
    const active = state.pageLimits.filter(row => row.daily_count>0);
    ui.byId("pageLimitsSummary").textContent = "已配置 " + state.pageLimits.length + " 个 Page；安排发布 " + active.length + " 个；每日合计上限 " + active.reduce((sum,row) => sum+row.daily_count,0) + " 条。";
    estimate();
  }

  function importPageLimits() {
    try {
      const seen = new Set();
      const rows = ui.byId("pageLimitsImport").value.trim().split(/\r?\n/).filter(Boolean).map(line => {
        const [id,count,tier = "",...name] = line.split("\t").map(value => value.trim());
        if (!/^[1-9][0-9]{0,30}$/.test(id) || seen.has(id) || !/^\d+$/.test(count) || Number(count)>24 || !["","A","B","C","hold"].includes(tier)) throw new Error("Page ID、次数、分档无效或存在重复行，请用 Tab 分隔。");
        seen.add(id);
        return {page_id:id,daily_count:Number(count),tier,name:name.join(" ")};
      });
      if (!rows.length || rows.length>1000) throw new Error("请导入1至1000个Page。");
      state.pageLimits = rows;
      renderPageLimits();
      ui.showToast("已替换列表，保存模板后生效。");
    } catch(error) { ui.showToast(error.message, true); }
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
    ui.byId("pageLimitsEnabled").addEventListener("change", renderPageLimits);
    ui.byId("pageLimitSearch").addEventListener("input", renderPageLimits);
    ui.byId("defaultDailyCount").addEventListener("input", estimate);
    ui.byId("importPageLimits").addEventListener("click", importPageLimits);
    ui.byId("pageLimitsBody").addEventListener("change", event => {
      const index = event.target.dataset.pageLimit;
      if (index === undefined) return;
      const value = Number(event.target.value);
      if (!Number.isInteger(value) || value<0 || value>24) { ui.showToast("每日次数必须为0至24的整数。",true); renderPageLimits(); return; }
      state.pageLimits[Number(index)].daily_count = value;
      renderPageLimits();
    });
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
