(() => {
  "use strict";

  const ui = window.FBAutoPublishUI;
  const state = {
    templates: [],
    total: 0,
    limit: 50,
    offset: 0,
    busyIds: new Set(),
    material: null,
    materialBusy: false,
    draftNamespace: "",
  };

  function isEnabled(item) {
    return String(item && item.status || "").toLowerCase() === "enabled";
  }

  function scheduleSummary(config) {
    const schedule = config && config.schedule || {};
    if (!["fixed", "random"].includes(schedule.mode)) return {frequency: "未设置", details: ""};
    const maximum = schedule.mode === "fixed" ? (schedule.times || []).length : Number(schedule.daily_count || 0);
    const custom = Array.isArray(config.page_daily_limits) || config.default_daily_count !== undefined;
    const rows = custom ? (config.page_daily_limits || []) : [];
    const counts = rows.map(row => Math.min(maximum, Number(row.daily_count || 0)));
    const fallback = Math.min(maximum, Number(config.default_daily_count ?? maximum));
    const activeCounts = [...new Set([...counts, fallback].filter(count => count > 0))].sort((a, b) => a - b);
    const frequency = activeCounts.length === 0
      ? "所有 Page 每天上限 0 次（暂停）"
      : activeCounts.length === 1
        ? "每个启用 Page 每天上限 " + activeCounts[0] + " 次"
        : "启用 Page 每天上限 " + activeCounts[0] + "–" + activeCounts[activeCounts.length - 1] + " 次（按 Page 配置）";
    const timing = schedule.mode === "fixed"
      ? "固定 " + maximum + " 个候选时段：" + (schedule.times || []).join(", ") + (config.stagger_minutes ? "（各时段后错峰 " + config.stagger_minutes + " 分钟）" : "")
      : "每日随机 " + maximum + " 个候选时刻：" + String(schedule.start || "—") + "–" + String(schedule.end || "—");
    const details = [timing + "（北京时间）"];
    if (custom) {
      details.push("已配置 " + counts.filter(count => count > 0).length + " 个 Page 安排发布、" + counts.filter(count => count === 0).length + " 个暂停");
      details.push("已配置 Page 合计上限 " + counts.reduce((sum, count) => sum + count, 0) + " 条/日");
      details.push(fallback ? "未列 Page 每天上限 " + fallback + " 次" : "未列 Page 暂停");
    }
    if (config.drama_cooldown_hours) details.push("同剧冷却 " + Number(config.drama_cooldown_hours) + "h");
    details.push("实际数量受授权、候选与冷却限制");
    return {frequency, details: details.join(" · ")};
  }

  async function loadGroups() {
    const data = await ui.api(ui.API_BASE + "/groups");
    const summary = data.summary || {};
    ui.byId("groupCount").textContent = summary.total_groups ?? 0;
    ui.byId("pageCount").textContent = summary.total_pages ?? 0;
    ui.byId("publishableCount").textContent = summary.publishable_pages ?? 0;
    ui.byId("missingCount").textContent = summary.missing_token_pages ?? 0;
  }

  function updatePager() {
    const page = Math.floor(state.offset / state.limit) + 1;
    const pages = Math.max(1, Math.ceil(state.total / state.limit));
    ui.byId("pageInfo").textContent = "第 " + page + " / " + pages + " 页，共 " + state.total + " 个模板";
    ui.byId("prevPage").disabled = state.offset <= 0;
    ui.byId("nextPage").disabled = state.offset + state.limit >= state.total;
  }

  function renderTemplates() {
    const body = ui.byId("templateRows");
    if (!state.templates.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty">暂无符合条件的模板，请点击“创建模板”开始配置。</td></tr>';
      updatePager();
      return;
    }
    body.innerHTML = state.templates.map(item => {
      const config = item.config || {};
      const schedule = scheduleSummary(config);
      const id = ui.positiveId(item.id || item.template_id);
      const version = ui.templateVersion(item);
      const enabled = isEnabled(item);
      const poolIds = Array.isArray(config.group_ids) ? config.group_ids.map(ui.escapeHtml).join(", ") : "";
      const busy = state.busyIds.has(id) ? " disabled" : "";
      const runDisabled = state.busyIds.has(id) || !enabled ? " disabled" : "";
      const runTitle = enabled ? "" : ' title="请先启用模板"';
      return [
        "<tr>",
        "<td><strong>", ui.escapeHtml(item.name || config.name || ("模板 " + id)), "</strong><br><small>v", version, " · ",
        config.video_template === "random_overlay" ? "随机排重模板" : "视频模板缺失", "</small></td>",
        "<td>", ui.escapeHtml(config.product || "—"), " / ", poolIds || "未选择 Page 池", "</td>",
        '<td><span class="badge ', enabled ? "success" : "warning", '">', enabled ? "已启用" : "已停用", "</span></td>",
        "<td><strong>", ui.escapeHtml(schedule.frequency), "</strong><br><small>", ui.escapeHtml(schedule.details), "</small></td>",
        '<td><div class="table-actions">',
        '<a class="button small link-button" href="/fb-auto-publish-template.html?v=20260820-list-only-v2&id=', id, '">编辑</a>',
        '<button class="button small" type="button" data-action="toggle" data-template-id="', id, '"', busy, ">", enabled ? "停用" : "启用", "</button>",
        '<button class="button small" type="button" data-action="run" data-template-id="', id, '"', runDisabled, runTitle, ">手动执行</button>",
        '<button class="button small primary" type="button" data-action="material" data-template-id="', id, '"', runDisabled, runTitle, ">爆款素材发布</button>",
        "</div></td>",
        "</tr>",
      ].join("");
    }).join("");
    updatePager();
  }

  async function loadTemplates() {
    const params = new URLSearchParams({
      limit: String(state.limit),
      offset: String(state.offset),
    });
    const query = ui.byId("filterQuery").value.trim();
    const status = ui.byId("filterStatus").value;
    if (query) params.set("q", query);
    if (status) params.set("status", status);
    ui.byId("templateRows").innerHTML = '<tr><td colspan="5" class="empty">正在加载模板…</td></tr>';
    try {
      const data = await ui.api(ui.API_BASE + "/templates?" + params.toString());
      state.templates = Array.isArray(data.items) ? data.items : [];
      state.total = Math.max(0, Number(data.total || 0));
      renderTemplates();
      return true;
    } catch (error) {
      state.templates = [];
      state.total = 0;
      ui.byId("templateRows").innerHTML = '<tr><td colspan="5" class="empty">模板列表加载失败：' + ui.escapeHtml(error.message || "请求失败") + "</td></tr>";
      updatePager();
      ui.showToast(error.message || "模板列表加载失败", true);
      return false;
    }
  }

  function findTemplate(id) {
    return state.templates.find(item => ui.positiveId(item.id || item.template_id) === id) || null;
  }

  const skipReasons = {
    fb_auto_page_frequency_limit: "Page 已暂停",
    fb_page_missing_eligible_token: "缺少有效发布授权",
    fb_auto_page_unknown_block: "历史发布结果待核验",
    fb_auto_material_active: "该素材已有待执行或执行中的任务",
    fb_auto_material_cooldown: "近 14 天已使用该素材",
    fb_auto_page_task_conflict: "该 Page 同一时点已有发布任务",
    page_paused: "Page 已暂停", paused_page: "Page 已暂停", page_daily_limit_zero: "Page 已暂停",
    missing_eligible_token: "缺少有效发布授权", page_unknown: "历史发布结果待核验",
    same_material_cooldown: "近 14 天已使用或正在使用该素材",
    same_material_reserved: "该素材已有待发布任务", material_reserved: "该素材已有待发布任务",
  };
  // A proxy can return 400 after the sidecar committed (for example if audit
  // logging fails). Only explicit pre-reservation business codes release a draft.
  const materialRejections = new Set([
    "fb_auto_version_conflict", "fb_auto_template_disabled", "fb_auto_video_template_required",
    "fb_auto_live_gate_closed", "fb_auto_prebuild_gate_closed", "fb_auto_page_pool_empty",
    "fb_auto_page_scope_invalid", "fb_auto_legacy_queue_conflict", "fb_auto_page_template_conflict",
    "fb_auto_capacity_snapshot_changed", "fb_auto_capacity_exceeded", "fb_auto_material_identity_invalid",
    "fb_auto_material_not_found", "fb_auto_source_data_invalid", "fb_auto_material_invalid",
    "fb_auto_material_url_invalid", "fb_auto_material_drama_unavailable", "fb_auto_material_blacklisted",
    "fb_auto_material_description_invalid", "fb_auto_material_duration_invalid", "fb_auto_material_probe_failed",
  ]);

  function materialStorageKey(id) {
    return "fb-hit-material:" + state.draftNamespace + ":" + id;
  }

  function materialStatus(message, isError = false) {
    ui.byId("materialStatus").textContent = message;
    ui.byId("materialStatus").className = "status-line" + (isError ? " error" : "");
  }

  function renderMaterialResult(result) {
    const queued = Number(result.queued || 0);
    const skipped = Number(result.skipped || 0);
    materialStatus("运行 #" + result.run_id + " 已创建：" + queued + " 个 Page 已加入发布队列，" + skipped + " 个跳过。" + (queued ? "发布进度请查看记录。" : "本轮没有可发布的 Page。"));
    ui.byId("materialResult").classList.remove("hidden");
    ui.byId("materialRunLink").href = "/fb-auto-publish-runs.html?run_id=" + ui.positiveId(result.run_id);
    const rows = Array.isArray(result.skipped_pages) ? result.skipped_pages : [];
    ui.byId("materialSkipped").classList.toggle("hidden", !rows.length);
    ui.byId("materialSkippedSummary").textContent = "查看 " + rows.length + " 个 Page 的跳过原因";
    ui.byId("materialSkippedRows").innerHTML = rows.map(row => "<tr><td>" + ui.escapeHtml(row.page_id) + "</td><td>" + ui.escapeHtml(row.message || skipReasons[row.reason] || row.reason || "暂不可发布") + "</td></tr>").join("");
    ui.byId("materialSubmit").classList.add("hidden");
    ui.byId("materialNew").classList.remove("hidden");
    ui.byId("materialClose").textContent = "关闭";
    ui.byId("hitMaterialId").readOnly = true;
  }

  function openMaterialDialog(item, fresh = false) {
    const id = ui.positiveId(item.id || item.template_id);
    let saved = null;
    try {
      saved = fresh ? null : JSON.parse(window.sessionStorage.getItem(materialStorageKey(id)) || "null");
      if (fresh) window.sessionStorage.removeItem(materialStorageKey(id));
    } catch (_error) { /* Submission will require durable browser storage. */ }
    state.material = {id, item, draft: saved && saved.draft || null, result: saved && saved.result || null};
    ui.byId("materialTemplateName").textContent = item.name || item.config?.name || "模板 " + id;
    ui.byId("hitMaterialId").value = state.material.draft?.material_id || "";
    ui.byId("hitMaterialId").readOnly = Boolean(state.material.draft);
    ui.byId("materialResult").classList.add("hidden");
    ui.byId("materialNew").classList.add("hidden");
    ui.byId("materialSubmit").classList.remove("hidden");
    ui.byId("materialSubmit").disabled = false;
    ui.byId("materialSubmit").textContent = state.material.draft ? "重试确认" : "确认发布";
    ui.byId("materialClose").textContent = "取消";
    materialStatus(state.material.draft ? "上次提交尚未确认结果，请重试确认同一轮任务。" : "确认后将开始制作并排队发布。", Boolean(state.material.draft));
    if (state.material.result) renderMaterialResult(state.material.result);
    if (!ui.byId("materialDialog").open) ui.byId("materialDialog").showModal();
    ui.byId("hitMaterialId").focus();
  }

  async function submitMaterial(event) {
    event.preventDefault();
    const current = state.material;
    if (!current || state.materialBusy || current.result) return;
    const materialId = ui.byId("hitMaterialId").value.trim();
    if (!/^[1-9][0-9]{0,19}$/.test(materialId)) {
      materialStatus("请输入一个有效的数字素材 ID。", true);
      return;
    }
    if (!current.draft) current.draft = {expected_version: ui.templateVersion(current.item), material_id: materialId, operation_id: ui.operationId()};
    try {
      window.sessionStorage.setItem(materialStorageKey(current.id), JSON.stringify({draft: current.draft}));
    } catch (_error) {
      current.draft = null;
      materialStatus("浏览器无法保存本次提交，请允许本站存储后重试。尚未发送发布请求。", true);
      return;
    }
    state.materialBusy = true;
    state.busyIds.add(current.id);
    ui.byId("materialSubmit").disabled = true;
    ui.byId("materialClose").disabled = true;
    ui.byId("hitMaterialId").readOnly = true;
    materialStatus("正在核对素材和 Page，并创建本轮任务…");
    renderTemplates();
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 125000);
    try {
      const response = await fetch(ui.API_BASE + "/templates/" + current.id + "/run-now", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(current.draft), signal: controller.signal,
      });
      const result = await response.json();
      if (!response.ok) {
        const error = new Error(result.message || "发布请求未完成");
        error.rejected = materialRejections.has(result.code || result.error);
        throw error;
      }
      if (result.ok !== true || !ui.positiveId(result.run_id)) throw new Error("未取得有效的运行记录");
      current.result = result;
      try { window.sessionStorage.setItem(materialStorageKey(current.id), JSON.stringify({draft: current.draft, result})); } catch (_error) { /* The original request remains replayable. */ }
      renderMaterialResult(result);
    } catch (error) {
      if (error.rejected) {
        current.draft = null;
        try { window.sessionStorage.removeItem(materialStorageKey(current.id)); } catch (_error) { /* Keep the visible error. */ }
        ui.byId("hitMaterialId").readOnly = false;
        ui.byId("materialSubmit").textContent = "确认发布";
        materialStatus(error.message, true);
      } else {
        ui.byId("materialSubmit").textContent = "重试确认";
        materialStatus("尚未确认提交结果。点击“重试确认”查询或继续同一轮任务，不会重复创建。", true);
      }
    } finally {
      window.clearTimeout(timeout);
      state.materialBusy = false;
      state.busyIds.delete(current.id);
      ui.byId("materialSubmit").disabled = false;
      ui.byId("materialClose").disabled = false;
      renderTemplates();
    }
  }

  async function handleAction(event) {
    const button = event.target.closest("button[data-action][data-template-id]");
    if (!button || !ui.byId("templateRows").contains(button)) return;
    const id = ui.positiveId(button.dataset.templateId);
    const item = findTemplate(id);
    if (!item || state.busyIds.has(id)) return;
    const action = button.dataset.action;
    const enabled = isEnabled(item);
    if (["run", "material"].includes(action) && !enabled) {
      ui.showToast("模板已停用，请先启用后再手动执行。", true);
      return;
    }
    if (action === "material") {
      openMaterialDialog(item);
      return;
    }
    const accepted = action === "run"
      ? await ui.confirmAction("确认手动执行", "将创建可追踪的手动计划，由后台异步冻结 Page 和素材。确认继续？")
      : await ui.confirmAction(enabled ? "确认停用模板" : "确认启用模板", enabled
        ? "停用后不再生成新时隙；未提交 Meta 的手动执行会取消，自动预制任务保留并暂停。正在提交 Meta 时需等待本次结果后再停用。"
        : "启用后系统会按模板时间创建 Page 发布任务；当天新启用从下一个完整北京时间自然日开始生成新时隙。已预制的同版本自动任务会继续。");
    if (!accepted) return;
    state.busyIds.add(id);
    renderTemplates();
    let listFailed = false;
    try {
      const version = ui.templateVersion(item);
      if (action === "run") {
        await ui.api(ui.API_BASE + "/templates/" + id + "/run-now", {
          method: "POST",
          body: JSON.stringify({
            expected_version: version,
            operation_id: ui.operationId(),
          }),
        });
        ui.showToast("手动执行计划已创建，可在发布记录中查看。");
      } else {
        await ui.api(ui.API_BASE + "/templates/" + id + "/" + (enabled ? "disable" : "enable"), {
          method: "POST",
          body: JSON.stringify({ expected_version: version }),
        });
        ui.showToast(enabled ? "模板已停用。" : "模板已启用。");
      }
      listFailed = !(await loadTemplates());
    } catch (error) {
      ui.showToast(error.message || "操作失败", true);
    } finally {
      state.busyIds.delete(id);
      if (!listFailed) renderTemplates();
    }
  }

  function bindEvents() {
    ui.byId("materialForm").addEventListener("submit", event => void submitMaterial(event));
    ui.byId("materialClose").addEventListener("click", () => {
      if (!state.materialBusy) ui.byId("materialDialog").close();
    });
    ui.byId("materialDialog").addEventListener("cancel", event => {
      if (state.materialBusy) event.preventDefault();
    });
    ui.byId("materialNew").addEventListener("click", () => {
      if (state.material && !state.materialBusy) openMaterialDialog(findTemplate(state.material.id) || state.material.item, true);
    });
    ui.byId("templateFilters").addEventListener("submit", event => {
      event.preventDefault();
      state.offset = 0;
      void loadTemplates();
    });
    ui.byId("resetFilters").addEventListener("click", () => {
      ui.byId("templateFilters").reset();
      state.offset = 0;
      void loadTemplates();
    });
    ui.byId("reloadTemplates").addEventListener("click", () => void loadTemplates());
    ui.byId("templateRows").addEventListener("click", event => void handleAction(event));
    ui.byId("prevPage").addEventListener("click", () => {
      state.offset = Math.max(0, state.offset - state.limit);
      void loadTemplates();
    });
    ui.byId("nextPage").addEventListener("click", () => {
      if (state.offset + state.limit >= state.total) return;
      state.offset += state.limit;
      void loadTemplates();
    });
  }

  void ui.boot({
    onReady: async auth => {
      const user = auth && auth.user || {};
      state.draftNamespace = String(user.tenant_key || "") + ":" + String(user.user_id || user.open_id || user.id || "current");
      bindEvents();
      await Promise.all([loadGroups(), loadTemplates()]);
    },
  });
})();
