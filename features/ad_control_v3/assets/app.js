(function () {
  "use strict";

  const page = document.body.dataset.v3Page || "";
  const bootstrap = readBootstrap();
  const apiBase = String(bootstrap.apiBase || "/api/ad-control/v3").replace(/\/$/, "");
  const OPERATOR_LABELS = {
    gt: "大于", gte: "大于等于", lt: "小于", lte: "小于等于", eq: "等于", ne: "不等于",
    between: "介于", in: "属于任一", not_in: "不属于", exists: "存在", not_exists: "不存在",
    contains: "包含", not_contains: "不包含", starts_with: "开头为", before: "早于", after: "晚于",
    within_last_days: "最近 X 天内", older_than_days: "早于 X 天前",
  };
  const RELATIVE_DAY_OPERATORS = new Set(["within_last_days", "older_than_days"]);
  const LEVEL_LABELS = { campaign: "Campaign", adset: "Ad Set", ad: "Ad" };
  const ACTION_LABELS = { pause: "关闭", copy: "复制" };
  const MODE_LABELS = { observe: "只观察", live: "正式执行" };
  const STATUS_LABELS = {
    pending: "等待中", running: "执行中", completed: "已完成", success: "成功", partial: "部分完成",
    observed: "已观察", blocked: "已阻断", failed: "失败", skipped: "已跳过", cancelled: "已取消", enabled: "已启用", disabled: "已停用",
  };

  const state = {
    auth: null,
    meta: null,
    actor: null,
    list: { page: 1, pageSize: 20, total: 0, items: [], loading: false },
    filters: {},
    editor: null,
    editorStep: 1,
    editorDirty: false,
    estimate: null,
    estimateLoading: false,
    estimateRequestSerial: 0,
    openMulti: "",
    openSingle: "",
    logs: { page: 1, pageSize: 20, total: 0, items: [], summary: {}, loading: false },
    logFilters: {},
    detail: null,
    detailLoading: false,
    requestSerial: 0,
  };
  const inFlight = new Set();

  document.addEventListener("DOMContentLoaded", init);

  function readBootstrap() {
    const element = document.getElementById("adControlV3Bootstrap");
    if (!element) return {};
    try {
      const value = JSON.parse(element.textContent || "{}");
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch (_error) {
      return {};
    }
  }

  function h(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function text(value, fallback) {
    const normalized = String(value == null ? "" : value).trim();
    return normalized || String(fallback == null ? "" : fallback);
  }

  function array(value) { return Array.isArray(value) ? value : []; }

  function asNumber(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : (fallback == null ? 0 : fallback);
  }

  function idOf(value) {
    if (!value || typeof value !== "object") return "";
    return text(value.id || value.group_id || value.rule_group_id || value.execution_id || value.preview_id);
  }

  function executionIdOf(value) {
    if (!value || typeof value !== "object") return "";
    return text(value.execution_id || value.preview_id || value.event_id || value.id);
  }

  function prettyDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return text(value, "—");
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(date).replace(/\//g, "-");
  }

  function formatCount(value) {
    const number = asNumber(value, 0);
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(number);
  }

  function option(value, label, selected, disabled) {
    return `<option value="${h(value)}"${selected ? " selected" : ""}${disabled ? " disabled" : ""}>${h(label)}</option>`;
  }

  function statusClass(status) {
    const key = text(status).toLowerCase();
    if (["success", "completed", "enabled", "active"].includes(key)) return "success";
    if (["partial", "warning", "skipped", "observed"].includes(key)) return "warning";
    if (["failed", "error", "blocked", "emergency_stopped"].includes(key)) return "danger";
    return "info";
  }

  function setPageStatus(kind, title, description, retryAction) {
    const element = document.getElementById("pageStatus");
    if (!element) return;
    element.hidden = false;
    element.className = `state-panel${kind === "error" ? " is-error" : ""}${kind === "empty" ? " is-empty" : ""}`;
    const icon = kind === "loading" ? '<span class="spinner" aria-hidden="true"></span>' : `<span class="empty-icon" aria-hidden="true">${kind === "error" ? "!" : "·"}</span>`;
    element.innerHTML = `${icon}<div><strong>${h(title)}</strong><p>${h(description)}</p>${retryAction ? `<button class="button button-small" type="button" data-action="${h(retryAction)}">重新加载</button>` : ""}</div>`;
  }

  function hidePageStatus() {
    const element = document.getElementById("pageStatus");
    if (element) element.hidden = true;
  }

  function toast(message, kind) {
    const region = document.getElementById("toastRegion");
    if (!region) return;
    const node = document.createElement("div");
    node.className = `toast${kind ? ` is-${kind}` : ""}`;
    node.textContent = text(message, "操作完成");
    region.appendChild(node);
    window.setTimeout(() => node.remove(), 4200);
  }

  async function api(path, options) {
    const settings = Object.assign({ method: "GET" }, options || {});
    const headers = new Headers(settings.headers || {});
    if (settings.body != null && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(`${apiBase}${path}`, {
      method: settings.method,
      headers,
      body: settings.body,
      credentials: "same-origin",
      cache: "no-store",
    });
    let payload = null;
    try { payload = await response.json(); } catch (_error) { payload = null; }
    if (!response.ok || (payload && payload.ok === false)) {
      const error = new Error(text(payload && (payload.message || payload.error), `请求失败（${response.status}）`));
      error.code = text(payload && payload.error, "request_failed");
      error.details = payload && payload.details;
      error.status = response.status;
      throw error;
    }
    return payload == null ? {} : payload;
  }

  async function rootApi(path, options) {
    const settings = Object.assign({ method: "GET" }, options || {});
    const headers = new Headers(settings.headers || {});
    if (settings.body != null && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(path, {
      method: settings.method,
      headers,
      body: settings.body,
      credentials: "same-origin",
      cache: "no-store",
    });
    let payload = null;
    try { payload = await response.json(); } catch (_error) { payload = null; }
    if (!response.ok || (payload && payload.ok === false)) {
      const error = new Error(text(payload && (payload.message || payload.error), `请求失败（${response.status}）`));
      error.code = text(payload && payload.error, "request_failed");
      error.details = payload && payload.details;
      error.status = response.status;
      throw error;
    }
    return payload == null ? {} : payload;
  }

  function requireSharedUi() {
    if (!window.UiTopbar) throw new Error("公共顶吸脚本 /ui-topbar.js 未加载");
    if (!window.QuickNav) throw new Error("公共快速导航脚本 /quick-nav.js 未加载");
  }

  function activeNavKey() {
    return page === "execution-logs" ? "adControlV3Logs" : "adControlV3Rules";
  }

  function quickNavOptions(auth) {
    return {
      container: document.getElementById("quickNav"),
      auth,
      activeKey: activeNavKey(),
      onNavigate: item => {
        if (item && item.href) void requestGuardedNavigation(item.href);
      },
    };
  }

  function renderWarmQuickNav() {
    const container = document.getElementById("quickNav");
    if (!window.QuickNav || !container) return;
    window.QuickNav.render(quickNavOptions(null)).catch(error => console.warn("预渲染快速导航失败", error));
  }

  async function loadSharedShell() {
    requireSharedUi();
    state.auth = await rootApi("/api/ui/topbar");
    window.UiTopbar.render({
      auth: state.auth,
      userCard: document.getElementById("userCard"),
      authButton: document.getElementById("authBtn"),
      refreshButton: document.getElementById("refreshBtn"),
      loginText: "登录",
      logoutText: "退出登录",
    });
    await window.QuickNav.render(quickNavOptions(state.auth));
    const authButton = document.getElementById("authBtn");
    if (authButton && !authButton.dataset.v3AuthBound) {
      authButton.dataset.v3AuthBound = "1";
      authButton.addEventListener("click", async () => {
        requireSharedUi();
        if (!(await allowShellNavigation())) return;
        return window.UiTopbar.handleAuthAction({
          auth: state.auth || {},
          api: rootApi,
          afterLogout: () => window.location.assign("/"),
        });
      });
    }
  }

  async function init() {
    bindGlobalEvents();
    setPageStatus("loading", page === "execution-logs" ? "正在加载执行日志" : "正在加载规则能力", "正在读取权限、产品和筛选字段。", "");
    try {
      requireSharedUi();
      renderWarmQuickNav();
      const [, payload] = await Promise.all([loadSharedShell(), api("/meta")]);
      state.meta = normalizeMeta(payload);
      state.actor = state.meta.actor;
      renderSystemCapabilityBanner();
      if (page === "rule-groups") {
        const root = document.getElementById("ruleGroupsApp");
        if (root) root.hidden = false;
        hidePageStatus();
        renderRuleGroupShell();
        await loadRuleGroups();
      } else if (page === "execution-logs") {
        const root = document.getElementById("executionLogsApp");
        if (root) root.hidden = false;
        hidePageStatus();
        renderLogShell();
        await loadExecutions();
      } else {
        throw new Error("未知页面");
      }
    } catch (error) {
      setPageStatus("error", "页面暂时无法使用", errorMessage(error), "retry-init");
    }
  }

  function normalizeMeta(payload) {
    const source = payload && payload.meta ? payload.meta : (payload || {});
    const actorSource = source.actor || source.user || source.current_user || {};
    const permissions = source.permissions || {};
    const role = text(actorSource.role || actorSource.user_role).toLowerCase();
    const isAdmin = actorSource.is_admin === true || source.is_admin === true || permissions.is_admin === true || ["admin", "administrator", "superadmin", "super_admin"].includes(role);
    const products = array(source.products || source.product_catalog).map(item => {
      const object = typeof item === "string" ? { value: item } : (item || {});
      const value = text(object.product_value || object.value || object.product || object.id);
      const evidence = object.evidence && typeof object.evidence === "object" ? object.evidence : {};
      return {
        value,
        label: text(object.label || object.display_name || object.name || evidence.display_name, value),
        enabled: object.enabled !== false,
        description: text(object.description || evidence.description || object.canonical_product),
        catalogKind: text(evidence.catalog_kind, "reporting_product"),
      };
    }).filter(item => item.value);
    const optimizers = array(source.optimizers || source.optimizer_options).map(item => {
      const object = item || {};
      const id = text(object.optimizer_id || object.id || object.value);
      return { id, name: text(object.name || object.username || object.label, id), email: text(object.email) };
    }).filter(item => item.id);
    const timezoneValues = array(source.account_timezones || source.timezones || source.timezone_options).map(item => {
      const object = typeof item === "string" ? { value: item } : (item || {});
      const value = text(object.value || object.time_zone || object.id || object.label);
      return { value, label: text(object.label, value) };
    }).filter(item => item.value);
    const rawFields = source.fields || source.field_catalog || source.condition_fields || [];
    let fields = [];
    if (Array.isArray(rawFields)) fields = rawFields;
    else if (rawFields && typeof rawFields === "object") {
      const direct = rawFields.facebook || rawFields.fb || rawFields.items || rawFields.all;
      fields = Array.isArray(direct) ? direct : Object.values(rawFields).flatMap(value => array(value));
    }
    const uniqueFields = new Map();
    fields.map(item => Object.assign({}, item, {
      key: text(item && (item.key || item.field || item.value)),
      label: text(item && (item.label || item.name), item && (item.key || item.field)),
      value_type: text(item && (item.value_type || item.type), "text"),
      levels: array(item && (item.levels || item.object_levels)),
      operators: array(item && item.operators),
      options: array(item && item.options),
    })).filter(item => item.key).forEach(item => { if (!uniqueFields.has(item.key)) uniqueFields.set(item.key, item); });
    fields = Array.from(uniqueFields.values());
    const currentOptimizerRaw = source.current_optimizer || actorSource.optimizer || {};
    const currentOptimizerId = text(source.current_optimizer_id || permissions.current_optimizer_id || actorSource.optimizer_id || currentOptimizerRaw.optimizer_id || currentOptimizerRaw.id);
    let currentOptimizer = optimizers.find(item => item.id === currentOptimizerId) || null;
    if (!currentOptimizer && currentOptimizerId) currentOptimizer = { id: currentOptimizerId, name: text(currentOptimizerRaw.name, currentOptimizerId), email: text(currentOptimizerRaw.email) };
    const actorName = text(actorSource.name || actorSource.username || actorSource.display_name, !isAdmin && currentOptimizer ? currentOptimizer.name : (isAdmin ? "管理员" : "当前优化师"));
    const capabilities = source.capabilities || {};
    const hasSearchFieldContract = Object.prototype.hasOwnProperty.call(capabilities, "rule_group_search_fields") || Object.prototype.hasOwnProperty.call(source, "rule_group_search_fields");
    const searchFields = array(capabilities.rule_group_search_fields || source.rule_group_search_fields).map(value => text(value).toLowerCase());
    const searchExplicitlyDisabled = capabilities.rule_group_search === false || capabilities.rule_group_keyword_search === false;
    const supportsRuleGroupSearch = !searchExplicitlyDisabled && (!hasSearchFieldContract || (searchFields.includes("name") && searchFields.includes("group_id")));
    const runner = source.runner || source.scheduler || {};
    const canEnable = permissions.can_enable === true;
    const canLiveExecute = permissions.can_live_execute === true;
    const schedulerAvailable = permissions.scheduler_available === true || runner.scheduler_available === true || runner.available === true || canEnable;
    return {
      actor: {
        id: text(actorSource.user_id || actorSource.id || actorSource.sub_user_id),
        name: actorName,
        email: text(actorSource.email), role: text(actorSource.role, isAdmin ? "admin" : "optimizer"),
        avatar: text(actorSource.avatar_url), isAdmin,
      },
      products, optimizers, timezones: timezoneValues, fields, currentOptimizer,
      channels: array(source.channels), objectLevels: array(source.object_levels),
      capabilities: Object.assign({}, capabilities, { supportsRuleGroupSearch }),
      permissions: {
        canEnable,
        canLiveExecute,
        schedulerAvailable,
        schedulerLiveEnabled: permissions.scheduler_live_enabled === true,
        livePauseEnabled: permissions.live_pause_enabled === true,
        liveCopyEnabled: permissions.live_copy_enabled === true,
        enableUnavailableReason: text(permissions.enable_unavailable_reason || runner.unavailable_reason, "计划调度器尚未发布"),
      },
    };
  }

  function capabilityBannerCopy(permissions) {
    const value = permissions || {};
    if (value.canEnable && value.schedulerLiveEnabled && value.canLiveExecute) {
      return {
        state: "live",
        title: "真实暂停、复制与自动调度已开放",
        description: "正式规则可调用 Meta 写接口；复制对象始终先以 PAUSED 创建并完成落表校验，再按发布开关激活。新规则仍默认停用 + 只观察。",
        badge: "正式执行可用",
      };
    }
    if (value.canEnable) {
      return {
        state: "observe",
        title: "持续观察调度已开放",
        description: value.canLiveExecute ? "观察规则可持续扫描；正式暂停与复制可先手动执行，自动写入仍受发布开关保护。" : "观察规则可持续扫描并记录命中对象，不调用 Meta 写接口。",
        badge: "观察调度可用",
      };
    }
    if (value.canLiveExecute) {
      return {
        state: "manual-live",
        title: "真实暂停与复制已开放，可先手动执行",
        description: `${text(value.enableUnavailableReason, "自动调度尚未开放")}。把规则保存为正式执行并完成试算后，可从列表点击“执行”。`,
        badge: "手动执行可用",
      };
    }
    return {
      state: "preview",
      title: "当前仅支持保存草稿 + 手动试算",
      description: `${text(value.enableUnavailableReason, "计划调度器尚未发布")}。规则不能启用，也不会持续自动扫描。`,
      badge: "手动试算",
    };
  }

  function renderSystemCapabilityBanner() {
    const banner = document.getElementById("systemCapabilityBanner");
    const title = document.getElementById("systemCapabilityTitle");
    const description = document.getElementById("systemCapabilityDescription");
    const badge = document.getElementById("systemCapabilityBadge");
    if (!banner || !title || !description || !badge || !state.meta) return;
    const copy = capabilityBannerCopy(state.meta.permissions);
    banner.dataset.releaseState = copy.state;
    title.textContent = copy.title;
    description.textContent = copy.description;
    badge.textContent = copy.badge;
  }

  function errorMessage(error) {
    const mapping = {
      optimizer_identity_unresolved: "当前账号尚未映射到有效优化师，请联系管理员处理。",
      optimizer_identity_ambiguous: "当前账号映射到多个优化师，系统已安全阻断。",
      optimizer_forbidden: "你无权设置其他优化师。",
      invalid_product_scope: "所选产品已停用或不属于短剧产品目录。",
      account_scope_forbidden: "新版规则只能按产品与优化师圈选，不能提交账号范围。",
      channel_not_enabled: "该渠道尚未开放，本期仅支持 Facebook。",
      field_not_supported: "当前层级不支持这个筛选字段，请重新选择。",
      stale_preview: "规则已变化或试算已过期，请重新试算。",
      live_pause_disabled: "正式关闭总开关尚未开启。",
      live_copy_disabled: "正式复制总开关尚未开启。",
      live_mode_required: "请先把规则组保存为“正式执行”，然后重新试算。",
      live_execute_confirm_required: "必须输入正式执行确认短语。",
      copy_schema_mismatch: "复制落表结构与来源表不一致，已在调用 Meta 前熔断。",
      missing_source_created_data: "命中对象没有可追溯的 created_data 来源记录，已阻止执行。",
      carrier_budget_not_independent: "所选承载结构无法独立设置预算，请改用独立 Ad Set 或独立 Campaign。",
      copy_persistence_not_configured: "复制落表合同尚未配置，正式复制已安全阻断。",
    };
    return mapping[error && error.code] || text(error && error.message, "请求失败，请稍后重试。");
  }

  function bindGlobalEvents() {
    document.addEventListener("click", handleClick);
    document.addEventListener("change", handleChange);
    document.addEventListener("input", handleInput);
    window.addEventListener("beforeunload", event => {
      if (!state.editorDirty && !isInFlight("editor-save")) return;
      event.preventDefault();
      event.returnValue = "";
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        if (state.openMulti) { state.openMulti = ""; renderCurrentPage(); }
        else if (state.detail) closeDetail();
        else closeDialog(false);
      }
    });
  }

  function renderCurrentPage() {
    renderSystemCapabilityBanner();
    if (page === "rule-groups") {
      if (state.editor) renderEditor(); else renderRuleGroupShell();
    } else if (page === "execution-logs") renderLogShell();
  }

  function queryString(filters, pageNumber, pageSize) {
    const params = new URLSearchParams();
    params.set("page", String(pageNumber));
    params.set("page_size", String(pageSize));
    Object.entries(filters || {}).forEach(([key, value]) => {
      if (Array.isArray(value)) value.filter(Boolean).forEach(item => params.append(key, String(item)));
      else if (value !== "" && value != null) params.set(key, String(value));
    });
    return params.toString();
  }

  function normalizeList(payload, keys) {
    const source = payload || {};
    let items = [];
    for (const key of keys) {
      if (Array.isArray(source[key])) { items = source[key]; break; }
    }
    if (!items.length && Array.isArray(source.data)) items = source.data;
    const pagination = source.pagination || source.page_info || {};
    return {
      items,
      total: asNumber(source.total != null ? source.total : pagination.total, items.length),
      page: asNumber(source.page != null ? source.page : pagination.page, 1),
      pageSize: asNumber(source.page_size != null ? source.page_size : pagination.page_size, 20),
      summary: source.summary || {},
    };
  }

  function emptyOption(label) { return option("", label, true, false); }

  function renderRuleGroupShell() {
    const root = document.getElementById("ruleGroupsApp");
    if (!root || state.editor) return;
    const optimizerFilter = state.actor && state.actor.isAdmin ? `
      <div class="field"><span class="field-label">优化师</span>${renderSearchableSingle("rule-optimizer", "全部优化师", state.meta.optimizers.map(item => ({ value: item.id, label: item.name, description: item.email })), String(state.filters.optimizer_id || ""), true)}</div>` : "";
    root.innerHTML = `
      <div class="toolbar">
        <div class="toolbar-copy"><h2>新版规则组</h2><p>范围由短剧产品与优化师共同确定。</p></div>
        <div class="toolbar-actions"><button class="button button-primary" type="button" data-action="new-group"><span aria-hidden="true">＋</span>新建规则组</button></div>
      </div>
      ${state.meta.permissions.canEnable ? "" : `<section class="scheduler-banner" role="status" aria-label="调度能力说明"><span class="scheduler-banner-icon" aria-hidden="true">i</span><div><strong>${state.meta.permissions.canLiveExecute ? "真实暂停与复制已开放，可先手动执行" : "当前仅支持保存草稿 + 手动试算"}</strong><p>${h(state.meta.permissions.enableUnavailableReason)}。${state.meta.permissions.canLiveExecute ? "把规则保存为正式执行并完成试算后，可从列表点击“执行”。" : "规则不能启用，也不会持续自动扫描。配置与试算结果仍会写入执行日志。"}</p></div><span class="pill pill-warning">${state.meta.permissions.canLiveExecute ? "手动 Canary" : "启用已锁定"}</span></section>`}
      <section class="filter-bar" aria-label="规则组筛选">
        ${state.meta.capabilities.supportsRuleGroupSearch ? `<div class="field"><label for="ruleFilterKeyword">搜索</label><input id="ruleFilterKeyword" type="search" placeholder="搜索规则组名称或 ID" autocomplete="off" data-filter="keyword" value="${h(state.filters.keyword || "")}"></div>` : ""}
        <div class="field"><span class="field-label">产品</span>${renderSearchableSingle("rule-product", "全部短剧产品", state.meta.products.map(item => ({ value: item.value, label: item.label, description: item.description, disabled: !item.enabled })), String(state.filters.product || ""), true)}</div>
        ${optimizerFilter}
        <div class="field"><label for="ruleFilterLevel">调控对象</label><select id="ruleFilterLevel" data-filter="object_level">
          ${option("", "全部层级", !state.filters.object_level)}
          ${Object.entries(LEVEL_LABELS).map(([value, label]) => option(value, label, state.filters.object_level === value)).join("")}
        </select></div>
        <div class="field"><label for="ruleFilterMode">运行模式</label><select id="ruleFilterMode" data-filter="run_mode">
          ${option("", "全部模式", !state.filters.run_mode)}${option("observe", "只观察", state.filters.run_mode === "observe")}${option("live", "正式执行", state.filters.run_mode === "live")}
        </select></div>
        <div class="field"><label for="ruleFilterState">状态</label><select id="ruleFilterState" data-filter="enabled">
          ${option("", "全部状态", state.filters.enabled == null || state.filters.enabled === "")}${option("true", "已启用", state.filters.enabled === "true")}${option("false", "已停用", state.filters.enabled === "false")}
        </select></div>
        <div class="filter-actions"><button class="button button-small" type="button" data-action="reset-rule-filters">清空</button></div>
      </section>
      <section class="panel table-panel" aria-labelledby="ruleListTitle">
        <div class="panel-header"><div><h2 id="ruleListTitle">规则组列表</h2><p id="ruleListCount">${state.list.loading ? "正在读取…" : `共 ${formatCount(state.list.total)} 个规则组`}</p></div><button class="button button-small" type="button" data-action="reload-groups"${state.list.loading ? " disabled" : ""}>刷新</button></div>
        <div id="ruleTableRegion" aria-live="polite">${renderRuleTable()}</div>
      </section>`;
  }

  function renderRuleTable() {
    if (state.list.loading) {
      return `<div class="table-scroll"><table aria-label="规则组加载中"><thead><tr><th>规则组</th><th>范围</th><th>对象</th><th>模式</th><th>状态</th><th>最近运行</th><th></th></tr></thead><tbody>${Array.from({ length: 6 }, () => `<tr><td><span class="skeleton">规则组加载中</span></td><td><span class="skeleton">产品</span></td><td><span class="skeleton">对象</span></td><td><span class="skeleton">模式</span></td><td><span class="skeleton">状态</span></td><td><span class="skeleton">时间</span></td><td></td></tr>`).join("")}</tbody></table></div>`;
    }
    if (!state.list.items.length) {
      return `<div class="empty-state"><div><span class="empty-icon" aria-hidden="true">规</span><h3>还没有符合条件的规则组</h3><p>新建规则时，所有业务字段保持空白；保存后固定停用并处于只观察模式。</p><button class="button button-primary" type="button" data-action="new-group">新建规则组</button></div></div>`;
    }
    const rows = state.list.items.map(group => {
      const id = idOf(group);
      const products = array(group.products || group.product_values);
      const optimizer = optimizerLabel(group.optimizer_id, group.optimizer_name);
      const enabled = group.enabled === true || group.enabled === 1;
      const stopped = group.emergency_stopped === true || group.emergency_stopped === 1;
      const toggleAvailable = canToggleGroup(enabled, state.meta.permissions);
      const toggleLabel = enabled ? "停用" : (toggleAvailable ? "启用" : "暂不可启用");
      const mutable = canMutateGroup(group, state.actor);
      const previewBusy = isInFlight(`preview:${id}`);
      const executeBusy = isInFlight(`execute:${id}`);
      const groupBusy = isInFlight(`group-write:${id}`);
      const readOnlyHint = "该规则组不属于当前用户，仅可查看";
      const latest = group.last_execution_at || group.last_preview_at || group.updated_at;
      const ruleCount = array(group.rules).length || asNumber(group.rule_count, 0);
      return `<tr>
        <td><div class="cell-title"><strong>${h(text(group.name, "未命名规则组"))}</strong><small>${h(id)} · ${ruleCount} 条规则${mutable ? "" : " · 只读"}</small></div></td>
        <td><div class="cell-stack"><div class="chip-row">${products.slice(0, 3).map(item => `<span class="chip" title="${h(item)}">${h(productLabel(item))}</span>`).join("")}${products.length > 3 ? `<span class="chip">+${products.length - 3}</span>` : ""}</div><small class="cell-muted">${h(optimizer)}</small></div></td>
        <td><span class="pill pill-info">${h(LEVEL_LABELS[group.object_level] || group.object_level || "—")}</span></td>
        <td><span class="pill ${group.run_mode === "live" ? "pill-warning" : "pill-safe"}">${h(MODE_LABELS[group.run_mode] || group.run_mode || "—")}</span></td>
        <td><div class="cell-stack">${stopped ? '<span class="pill pill-danger">已急停</span>' : `<span class="pill ${enabled ? "pill-success" : "pill-muted"}">${enabled ? "已启用" : "已停用"}</span>`}${group.preview_valid === false ? '<small class="cell-muted">需重新试算</small>' : ""}</div></td>
        <td><div class="cell-stack"><span>${h(prettyDate(latest))}</span><small class="cell-muted">${h(text(group.last_execution_status || group.last_preview_status, "暂无记录"))}</small></div></td>
        <td><div class="cell-actions">
          <button class="button button-small" type="button" data-action="edit-group" data-id="${h(id)}"${mutable && !groupBusy ? "" : ` disabled aria-disabled="true" title="${h(mutable ? "操作处理中" : readOnlyHint)}"`}>编辑</button>
          <button class="button button-small" type="button" data-action="preview-group" data-id="${h(id)}"${mutable && !previewBusy && !groupBusy ? "" : ` disabled aria-disabled="true" title="${h(mutable ? "试算处理中" : readOnlyHint)}"`}>${previewBusy ? "试算中…" : "试算"}</button>
          ${group.run_mode === "live" && state.meta.permissions.canLiveExecute ? `<button class="button button-small button-danger" type="button" data-action="execute-group" data-id="${h(id)}"${mutable && !executeBusy && !groupBusy ? "" : ` disabled aria-disabled="true" title="${h(mutable ? "执行处理中" : readOnlyHint)}"`}>${executeBusy ? "执行中…" : "执行"}</button>` : ""}
          <button class="icon-button" type="button" title="${h(mutable ? "复制规则组" : readOnlyHint)}" aria-label="复制规则组 ${h(text(group.name))}" data-action="duplicate-group" data-id="${h(id)}"${mutable && !groupBusy ? "" : ' disabled aria-disabled="true"'}>⧉</button>
          <button class="icon-button" type="button" title="${h(!mutable ? readOnlyHint : (toggleAvailable ? toggleLabel : "计划调度器尚未发布：当前仅支持保存草稿和手动试算"))}" aria-label="${h(toggleLabel)} ${h(text(group.name))}" data-action="toggle-group" data-id="${h(id)}" data-enabled="${enabled ? "true" : "false"}" data-mode="${h(group.run_mode || "observe")}"${mutable && toggleAvailable && !groupBusy ? "" : ` disabled aria-disabled="true"${!toggleAvailable ? ' data-enable-blocked="true"' : ""}`}>${enabled ? "停" : "启"}</button>
          ${enabled ? `<button class="icon-button" type="button" title="${h(mutable ? "紧急停止" : readOnlyHint)}" aria-label="紧急停止 ${h(text(group.name))}" data-action="emergency-group" data-id="${h(id)}"${mutable && !groupBusy ? "" : ' disabled aria-disabled="true"'}>!</button>` : ""}
          <button class="icon-button" type="button" title="${h(mutable ? "删除" : readOnlyHint)}" aria-label="删除 ${h(text(group.name))}" data-action="delete-group" data-id="${h(id)}"${mutable && !groupBusy ? "" : ' disabled aria-disabled="true"'}>×</button>
        </div></td>
      </tr>`;
    }).join("");
    const start = state.list.total ? (state.list.page - 1) * state.list.pageSize + 1 : 0;
    const end = Math.min(state.list.page * state.list.pageSize, state.list.total);
    const pageCount = Math.max(1, Math.ceil(state.list.total / state.list.pageSize));
    return `<div class="table-scroll"><table aria-label="规则组列表"><thead><tr><th>规则组</th><th>产品与优化师</th><th>调控对象</th><th>运行模式</th><th>状态</th><th>最近活动</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="pagination"><p>显示 ${start}–${end} / ${formatCount(state.list.total)}</p><div class="pagination-controls"><button class="button button-small" type="button" data-action="rule-page-prev"${state.list.page <= 1 ? " disabled" : ""}>上一页</button><span class="pill">${state.list.page} / ${pageCount}</span><button class="button button-small" type="button" data-action="rule-page-next"${state.list.page >= pageCount ? " disabled" : ""}>下一页</button></div></div>`;
  }

  function optimizerLabel(optimizerId, optimizerName) {
    const id = text(optimizerId);
    const found = state.meta && state.meta.optimizers.find(item => item.id === id);
    return text(optimizerName || (found && found.name), id ? `优化师 ${id}` : "未解析优化师");
  }

  function productLabel(productValue) {
    const value = text(productValue);
    const found = state.meta && state.meta.products.find(item => item.value === value);
    return text(found && found.label, value || "未解析产品");
  }

  function canToggleGroup(enabled, permissions) {
    return Boolean(enabled) || Boolean(permissions && permissions.canEnable);
  }

  function canMutateGroup(group, actor) {
    if (actor && actor.isAdmin) return true;
    if (!group || !actor) return false;
    if (group.can_mutate != null) return group.can_mutate === true || group.can_mutate === 1;
    const ownerId = text(group.owner_user_id || group.owner_id || group.created_by_user_id);
    return Boolean(ownerId && text(actor.id) && ownerId === text(actor.id));
  }

  function beginInFlight(key) {
    const normalized = text(key);
    if (!normalized || inFlight.has(normalized)) return false;
    inFlight.add(normalized);
    return true;
  }

  function endInFlight(key) { inFlight.delete(text(key)); }
  function isInFlight(key) { return inFlight.has(text(key)); }
  function listedGroup(id) { return state.list.items.find(group => idOf(group) === text(id)) || null; }

  function scopeFingerprint(editor) {
    const source = editor || {};
    const selection = source.selection || {};
    return JSON.stringify({
      channel: text(source.channel), optimizer_id: text(source.optimizer_id), object_level: text(source.object_level),
      products: array(source.products).map(String).sort(), account_timezones: array(source.account_timezones).map(String).sort(),
      metric_window_days: text(selection.metric_window_days),
    });
  }

  function invalidateEstimate() {
    state.estimateRequestSerial += 1;
    state.estimate = null;
    state.estimateLoading = false;
  }

  async function loadRuleGroups() {
    const serial = ++state.requestSerial;
    state.list.loading = true;
    renderRuleGroupShell();
    try {
      const payload = await api(`/rule-groups?${queryString(state.filters, state.list.page, state.list.pageSize)}`);
      if (serial !== state.requestSerial) return;
      const normalized = normalizeList(payload, ["items", "rule_groups", "rows"]);
      Object.assign(state.list, normalized, { loading: false });
    } catch (error) {
      if (serial !== state.requestSerial) return;
      state.list.loading = false;
      toast(errorMessage(error), "error");
    }
    renderRuleGroupShell();
  }

  function newEditor() {
    const current = state.meta.currentOptimizer;
    return {
      id: "", version: "", name: "", description: "", channel: "", object_level: "", run_mode: "observe",
      optimizer_id: state.actor.isAdmin ? "" : text(current && current.id), products: [], account_timezones: [],
      rules: [], schedule: {}, quotas: {}, selection: {}, enabled: false, emergency_stopped: false, owner_user_id: text(state.actor.id), can_mutate: true,
    };
  }

  function normalizeGroupForEditor(raw) {
    const group = raw && raw.rule_group ? raw.rule_group : (raw || {});
    return {
      id: idOf(group), version: text(group.config_version || group.version), name: text(group.name), description: text(group.description),
      channel: text(group.channel), object_level: text(group.object_level), run_mode: text(group.run_mode, "observe"),
      optimizer_id: text(group.optimizer_id), products: array(group.products || group.product_values).map(String),
      account_timezones: array(group.account_timezones).map(String), rules: array(group.rules).map((rule, index) => normalizeRuleForEditor(rule, index)),
      schedule: Object.assign({}, group.schedule || {}), quotas: Object.assign({}, group.quotas || {}), selection: Object.assign({}, group.selection || {}),
      enabled: group.enabled === true || group.enabled === 1, emergency_stopped: group.emergency_stopped === true || group.emergency_stopped === 1,
      owner_user_id: text(group.owner_user_id || group.owner_id || group.created_by_user_id), can_mutate: group.can_mutate,
    };
  }

  function normalizeRuleForEditor(rule, index) {
    return {
      rule_id: text(rule && (rule.rule_id || rule.id), createRuleId(index)), name: text(rule && rule.name),
      priority: rule && rule.priority != null ? String(rule.priority) : "", logic: text(rule && rule.logic), action: text(rule && rule.action),
      conditions: array(rule && rule.conditions).map(condition => ({ field: text(condition.field), operator: text(condition.operator), value: condition.value })),
      copy_parameters: Object.assign({}, rule && rule.copy_parameters || {}),
    };
  }

  function createRuleId(index) {
    return `rule-${Date.now().toString(36)}-${String(index + 1)}-${Math.random().toString(36).slice(2, 7)}`;
  }

  function newRuleDraft(index) {
    return {
      rule_id: createRuleId(index), name: "", priority: "", logic: "", action: "",
      conditions: [{ field: "", operator: "", value: "" }], copy_parameters: {},
    };
  }

  function renderEditor() {
    const root = document.getElementById("ruleGroupsApp");
    if (!root || !state.editor) return;
    const steps = [
      [1, "范围与归属"], [2, "对象与模式"], [3, "筛选与动作"], [4, "计划与额度"], [5, "检查与试算"],
    ];
    const editorBusy = isInFlight("editor-save");
    root.innerHTML = `<div class="editor">
      <aside class="editor-rail" aria-label="规则配置步骤">
        <div class="editor-rail-head"><strong>${state.editor.id ? "编辑规则组" : "新建规则组"}</strong><small>所有业务输入初始为空</small></div>
        <nav class="step-nav">${steps.map(([number, label]) => `<button class="step-button${state.editorStep === number ? " is-active" : ""}${state.editorStep > number ? " is-complete" : ""}" type="button" data-action="goto-step" data-step="${number}"${state.editorStep === number ? ' aria-current="step"' : ""}${editorBusy ? " disabled" : ""}><span class="step-number">${state.editorStep > number ? "✓" : number}</span><strong>${label}</strong></button>`).join("")}</nav>
      </aside>
      <div class="editor-main">
        <header class="editor-head"><div><p class="eyebrow">RULE GROUP CONFIGURATION</p><h2>${state.editor.id ? h(text(state.editor.name, "未命名规则组")) : "创建规则组"}</h2><p>${state.editor.id ? `规则组 ID：${h(state.editor.id)}` : "逐步设置范围、条件和计划，保存草稿不会启动调控。"}</p></div><button class="button button-quiet" type="button" data-action="close-editor"${editorBusy ? " disabled" : ""}>返回列表</button></header>
        <div id="editorPane" aria-busy="${editorBusy ? "true" : "false"}"><fieldset class="editor-pane-fieldset"${editorBusy ? " disabled" : ""}>${renderEditorStep()}</fieldset></div>
        <footer class="editor-footer">
          <div><button class="button" type="button" data-action="previous-step"${state.editorStep <= 1 || editorBusy ? " disabled" : ""}>上一步</button><span class="save-note">第 ${state.editorStep} / 5 步 · 保存后固定停用</span></div>
          <div><button class="button" type="button" data-action="save-draft"${editorBusy ? " disabled" : ""}>${editorBusy ? "处理中…" : "保存草稿"}</button>${state.editorStep < 5 ? `<button class="button button-primary" type="button" data-action="next-step"${editorBusy ? " disabled" : ""}>下一步</button>` : `<button class="button button-primary" type="button" data-action="save-preview"${editorBusy ? " disabled" : ""}>${editorBusy ? "保存并试算中…" : "保存并立即试算"}</button>`}</div>
        </footer>
      </div>
    </div>`;
  }

  function renderEditorStep() {
    if (state.editorStep === 1) return renderScopeStep();
    if (state.editorStep === 2) return renderObjectStep();
    if (state.editorStep === 3) return renderRulesStep();
    if (state.editorStep === 4) return renderScheduleStep();
    return renderReviewStep();
  }

  function renderScopeStep() {
    const editor = state.editor;
    const isAdmin = state.actor.isAdmin;
    const optimizer = state.meta.optimizers.find(item => item.id === String(editor.optimizer_id)) || state.meta.currentOptimizer;
    return `<section class="step-pane" aria-labelledby="scopeStepTitle">
      <div class="section-card"><div class="section-head"><div><h3 id="scopeStepTitle">基本信息</h3><p>名称和说明只用于识别规则组，不参与广告筛选。</p></div><span class="pill pill-safe">新建后停用</span></div>
        <div class="section-body"><div class="form-grid">
          <div class="field"><label for="groupName">规则组名称 <span class="required" aria-hidden="true">*</span></label><input id="groupName" type="text" maxlength="128" autocomplete="off" placeholder="例如：爆款剧高 ROAS 放量规则" value="${h(editor.name)}" data-bind="name"></div>
          <div class="field"><label for="groupDescription">说明</label><input id="groupDescription" type="text" maxlength="1000" autocomplete="off" placeholder="说明用途、适用场景或负责人" value="${h(editor.description)}" data-bind="description"></div>
        </div></div>
      </div>
      <div class="section-card"><div class="section-head"><div><h3>投放渠道</h3><p>本期仅 Facebook 可用，TikTok 的结构已预留。</p></div></div>
        <div class="section-body"><div class="choice-grid channel-grid">
          <button class="channel-card${editor.channel === "facebook" ? " is-selected" : ""}" type="button" data-action="select-channel" data-value="facebook"><span class="choice-kicker">AVAILABLE</span><strong>Facebook / Meta</strong><p>支持 Campaign、Ad Set 与 Ad 的筛选和观察试算。</p><span class="choice-check" aria-hidden="true">✓</span></button>
          <button class="channel-card" type="button" disabled aria-disabled="true"><span class="choice-kicker">ROADMAP</span><strong>TikTok</strong><p>渠道适配器已预留，本期不可保存、试算或执行。</p><span class="pill pill-muted">暂未开放</span></button>
        </div></div>
      </div>
      <div class="section-card"><div class="section-head"><div><h3>业务范围</h3><p>产品与优化师会同时进入服务端查询条件；广告账号不是配置项。</p></div></div>
        <div class="section-body"><div class="form-grid">
          <div class="field"><span class="field-label">优化师 <span class="required" aria-hidden="true">*</span></span>
            ${isAdmin ? `${renderSearchableSingle("editor-optimizer", "请选择优化师", state.meta.optimizers.map(item => ({ value: item.id, label: item.name, description: item.email })), String(editor.optimizer_id || ""))}<p class="field-hint">支持按姓名、邮箱或优化师 ID 搜索；选择会记录在审计信息中。</p>` : `<div class="locked-value"><span><strong>${h(text(optimizer && optimizer.name, "正在解析本人优化师"))}</strong><small>${h(text(optimizer && optimizer.email, editor.optimizer_id ? `优化师 ID ${editor.optimizer_id}` : "由服务端身份唯一映射"))}</small></span><span class="pill pill-safe">仅本人</span></div><p class="field-hint">普通优化师无法在客户端更改此范围，服务端会再次校验。</p>`}
          </div>
          <div class="field"><span class="field-label">短剧产品 <span class="required" aria-hidden="true">*</span></span>${renderMultiSelect("products", "选择一个或多个短剧产品", state.meta.products.map(item => ({ value: item.value, label: item.label, description: item.description, disabled: !item.enabled })), editor.products)}</div>
          <div class="field field-span-2"><span class="field-label">账户时区（可选）</span>${renderMultiSelect("account_timezones", "不选择则不限制账户时区", state.meta.timezones, editor.account_timezones)}<p class="field-hint">留空时不生成时区筛选。设置后，时区缺失的广告会被跳过并记录原因；计划仍按各账户本地时间判断。</p></div>
        </div>
        </div>
      </div>
    </section>`;
  }

  function renderMultiSelect(name, placeholder, options, selected) {
    const values = array(selected).map(String);
    const labels = values.map(value => {
      const found = array(options).find(item => String(item.value) === value);
      return found ? found.label : value;
    });
    const open = state.openMulti === name;
    return `<div class="multi-select" data-multi-root="${h(name)}">
      <button class="multi-trigger" type="button" data-action="toggle-multi" data-name="${h(name)}" aria-haspopup="listbox" aria-expanded="${open ? "true" : "false"}">
        <span class="multi-value">${labels.length ? labels.slice(0, 4).map(label => `<span class="chip">${h(label)}</span>`).join("") + (labels.length > 4 ? `<span class="chip">+${labels.length - 4}</span>` : "") : `<span class="multi-trigger-placeholder">${h(placeholder)}</span>`}</span><span aria-hidden="true">⌄</span>
      </button>
      ${open ? `<div class="multi-menu"><div class="multi-search"><input type="search" placeholder="搜索选项" autocomplete="off" data-multi-search="${h(name)}" aria-label="搜索${h(placeholder)}"></div><div class="multi-options" role="listbox" aria-multiselectable="true">${array(options).length ? array(options).map(item => {
        const value = String(item.value); const chosen = values.includes(value); return `<button class="tag-option${chosen ? " is-selected" : ""}" type="button" role="option" aria-selected="${chosen ? "true" : "false"}" data-action="multi-option" data-name="${h(name)}" data-value="${h(value)}" data-search-text="${h(`${item.label || value} ${item.description || ""}`.toLowerCase())}"${item.disabled ? " disabled" : ""}><span class="tag-check" aria-hidden="true">✓</span><span>${h(item.label || value)}${item.description ? `<small>${h(item.description)}</small>` : ""}</span></button>`;
      }).join("") : '<div class="condition-empty">暂无可用选项</div>'}</div></div>` : ""}
    </div>`;
  }

  function renderSearchableSingle(name, placeholder, options, selected, allowClear) {
    const value = String(selected || "");
    const found = array(options).find(item => String(item.value) === value);
    const open = state.openSingle === name;
    const visibleLabel = found ? (found.label || found.value) : "";
    const availableOptions = array(options);
    return `<div class="multi-select single-select" data-single-root="${h(name)}">
      <button class="multi-trigger" type="button" data-action="toggle-single" data-name="${h(name)}" aria-label="${h(placeholder)}" aria-haspopup="listbox" aria-expanded="${open ? "true" : "false"}">
        <span class="multi-value">${visibleLabel ? `<span class="single-value"><strong>${h(visibleLabel)}</strong>${found.description ? `<small>${h(found.description)}</small>` : ""}</span>` : `<span class="multi-trigger-placeholder">${h(placeholder)}</span>`}</span><span aria-hidden="true">⌄</span>
      </button>
      ${open ? `<div class="multi-menu single-menu"><div class="multi-search"><input type="search" placeholder="输入姓名、邮箱、ID 或产品名" autocomplete="off" data-single-search="${h(name)}" aria-label="搜索${h(placeholder)}"></div><div class="multi-options" role="listbox">
        ${allowClear ? `<button class="tag-option${value ? "" : " is-selected"}" type="button" role="option" aria-selected="${value ? "false" : "true"}" data-action="single-option" data-name="${h(name)}" data-value="" data-search-text="全部 清空"><span class="tag-check" aria-hidden="true">✓</span><span>${h(placeholder)}</span></button>` : ""}
        ${availableOptions.length ? availableOptions.map(item => {
          const itemValue = String(item.value); const chosen = itemValue === value;
          return `<button class="tag-option${chosen ? " is-selected" : ""}" type="button" role="option" aria-selected="${chosen ? "true" : "false"}" data-action="single-option" data-name="${h(name)}" data-value="${h(itemValue)}" data-search-text="${h(`${item.label || itemValue} ${item.description || ""} ${itemValue}`.toLowerCase())}"${item.disabled ? " disabled" : ""}><span class="tag-check" aria-hidden="true">✓</span><span>${h(item.label || itemValue)}${item.description ? `<small>${h(item.description)}</small>` : ""}</span></button>`;
        }).join("") : '<div class="condition-empty">暂无可用选项</div>'}
      </div></div>` : ""}
    </div>`;
  }

  function renderEstimateMetrics() {
    if (!state.estimate) return "";
    const source = state.estimate.summary || state.estimate;
    const metrics = [
      ["产品", source.product_count != null ? source.product_count : state.editor.products.length],
      ["广告账户", source.account_count != null ? source.account_count : source.ad_account_count],
      ["Campaign", source.campaign_count], ["Ad Set", source.adset_count], ["Ad", source.ad_count], ["目标对象", source.object_count != null ? source.object_count : source.target_count],
    ].filter(item => item[1] != null);
    return metrics.length ? `<div class="estimate-metrics">${metrics.map(([label, value]) => `<div class="mini-metric"><strong>${h(formatCount(value))}</strong><span>${h(label)}</span></div>`).join("")}</div>` : "";
  }

  function renderObjectStep() {
    const editor = state.editor;
    const schedulerAvailable = state.meta.permissions.canEnable;
    return `<section class="step-pane" aria-labelledby="objectStepTitle">
      <div class="section-card"><div class="section-head"><div><h3 id="objectStepTitle">调控对象</h3><p>一个规则组只能处理一个 Meta 对象层级。层级变化会清理不兼容条件。</p></div></div>
        <div class="section-body"><div class="choice-grid">
          ${levelChoice("campaign", "广告系列", "使用 Campaign 聚合效果，关闭会影响其下全部 Ad Set 与 Ad。")}
          ${levelChoice("adset", "广告组", "筛选和处理 Ad Set，保留同 Campaign 下其他广告组。")}
          ${levelChoice("ad", "广告", "精确到单条广告，保留同 Ad Set 下其他广告。")}
        </div></div>
      </div>
      <div class="section-card"><div class="section-head"><div><h3>运行模式</h3><p>运行模式与命中动作分开设置。“观察”不会调用 Meta 写接口。</p></div></div>
        <div class="section-body"><div class="mode-box">
          <label class="mode-option is-safe"><input type="radio" name="runMode" value="observe" data-bind="run_mode"${editor.run_mode === "observe" ? " checked" : ""}><span><strong>只观察</strong><p>${schedulerAvailable ? "持续扫描并记录本来会关闭或复制的对象，不执行外部写操作。" : "当前可保存观察配置并手动试算；计划调度器发布前不会持续自动扫描。"}</p></span></label>
          <label class="mode-option is-risk"><input type="radio" name="runMode" value="live" data-bind="run_mode"${editor.run_mode === "live" ? " checked" : ""}><span><strong>正式执行</strong><p>真实暂停与复制仍需有效试算、逐次确认和服务端总开关；复制会先创建为 PAUSED 并完成落表校验。</p></span></label>
        </div><div class="safe-default"><span aria-hidden="true">✓</span><div><strong>安全默认已生效</strong><p>${schedulerAvailable ? "新规则组由服务端强制保存为“停用 + 只观察”。即使在这里选择正式执行，保存本身也不会启用。" : "新规则组固定停用；本期只能保存草稿和手动试算，启用入口已锁定。"}</p></div></div></div>
      </div>
      <div class="section-card"><div class="section-head"><div><h3>结构范围估算</h3><p>这里只读取对象身份并返回结构数量，不判断规则指标，也不会写入 Meta。</p></div></div><div class="section-body">
        <div class="estimate-card"><div><h4>${editor.object_level ? `${h(LEVEL_LABELS[editor.object_level])} 范围` : "请先选择调控对象"}</h4><p>最终可命中数以保存后的手动试算为准。请填写指标窗口后再估算。</p><div class="field estimate-window"><label for="scopeMetricWindow">指标窗口 <span class="required" aria-hidden="true">*</span></label><div class="input-with-unit"><input id="scopeMetricWindow" type="number" min="1" inputmode="numeric" placeholder="输入最近天数" value="${h(editor.selection.metric_window_days || "")}" data-selection="metric_window_days"><span>天</span></div></div>${renderEstimateMetrics()}</div><button class="button" type="button" data-action="estimate-scope"${state.estimateLoading || !editor.object_level ? " disabled" : ""}>${state.estimateLoading ? "估算中…" : "估算当前范围"}</button></div>
      </div></div>
    </section>`;
  }

  function levelChoice(value, title, description) {
    const selected = state.editor.object_level === value;
    return `<button class="level-card${selected ? " is-selected" : ""}" type="button" data-action="select-level" data-value="${value}"><span class="choice-kicker">${h(LEVEL_LABELS[value])}</span><strong>${h(title)}</strong><p>${h(description)}</p><span class="choice-check" aria-hidden="true">✓</span></button>`;
  }

  function fieldsForLevel(includeRoadmap) {
    const level = state.editor && state.editor.object_level;
    return state.meta.fields.filter(field => (!field.levels.length || !level || field.levels.includes(level)) && (includeRoadmap || (field.filterable !== false && field.previewable !== false)));
  }

  function renderFieldCatalog() {
    const groups = {};
    fieldsForLevel(true).forEach(field => {
      const source = text(field.source, "其他");
      const label = source === "custom_source_insight" ? "业务与效果数据" : source === "computed" ? "计算指标" : source === "meta" ? "Meta 对象信息（后续能力）" : "其他字段";
      if (!groups[label]) groups[label] = [];
      groups[label].push(field);
    });
    if (!state.editor.object_level) return '<div class="condition-empty">请先选择调控对象，字段目录会随层级变化。</div>';
    return `<div class="catalog">${Object.entries(groups).map(([label, fields]) => `<details class="catalog-group"${label === "业务与效果数据" ? " open" : ""}><summary><span>${h(label)}</span><span class="pill">${fields.length} 项</span></summary><div class="catalog-items">${fields.map(field => `<div class="catalog-item"><strong>${h(field.label)}</strong><small>${field.filterable !== false && field.previewable !== false ? `可筛选 · ${h(field.value_type)}` : "仅展示 · 暂不可试算"}</small></div>`).join("")}</div></details>`).join("")}</div>`;
  }

  function renderRulesStep() {
    const level = state.editor.object_level;
    return `<section class="step-pane" aria-labelledby="rulesStepTitle">
      <div class="section-card"><div class="section-head"><div><h3 id="rulesStepTitle">筛选规则与命中动作</h3><p>规则内条件可以使用 AND / OR；同一对象命中关闭和复制时，服务端始终以关闭优先。</p></div><button class="button button-small" type="button" data-action="add-rule"${level ? "" : " disabled"}>＋ 添加规则</button></div>
        <div class="section-body">${!level ? '<div class="condition-empty">请先在上一步选择 Campaign、Ad Set 或 Ad。</div>' : state.editor.rules.length ? `<div class="rules-list">${state.editor.rules.map(renderRuleCard).join("")}</div>` : '<div class="empty-state"><div><span class="empty-icon" aria-hidden="true">＋</span><h3>还没有筛选规则</h3><p>添加规则后，明确选择命中动作并配置至少一个条件。</p><button class="button" type="button" data-action="add-rule">添加第一条规则</button></div>'}</div>
      </div>
      ${level ? `<div class="section-card"><div class="section-head"><div><h3>可用筛选字段</h3><p>当前为 ${h(LEVEL_LABELS[level])} 层级；灰掉的规划字段不会出现在条件下拉框。</p></div></div><div class="section-body">${renderFieldCatalog()}</div></div>` : ""}
    </section>`;
  }

  function renderRuleCard(rule, index) {
    const fields = fieldsForLevel(false);
    return `<article class="rule-card" data-rule-index="${index}">
      <header class="rule-head"><div><span class="rule-number">${index + 1}</span><strong>${h(text(rule.name, `规则 ${index + 1}`))}</strong></div><button class="icon-button" type="button" aria-label="删除规则 ${index + 1}" title="删除规则" data-action="remove-rule" data-index="${index}">×</button></header>
      <div class="rule-body">
        <div class="form-grid four">
          <div class="field"><label for="ruleName${index}">规则名称</label><input id="ruleName${index}" type="text" maxlength="128" autocomplete="off" placeholder="例如：ROAS 达标且消耗充分" value="${h(rule.name)}" data-rule-field="name" data-index="${index}"></div>
          <div class="field"><label for="rulePriority${index}">优先级</label><input id="rulePriority${index}" type="number" min="0" max="100000" inputmode="numeric" placeholder="数字越小越先判断" value="${h(rule.priority)}" data-rule-field="priority" data-index="${index}"></div>
          <div class="field"><label for="ruleLogic${index}">条件关系 <span class="required" aria-hidden="true">*</span></label><select id="ruleLogic${index}" data-rule-field="logic" data-index="${index}"><option value="">请选择 AND 或 OR</option>${option("and", "全部满足（AND）", rule.logic === "and")}${option("or", "任一满足（OR）", rule.logic === "or")}</select></div>
          <div class="field"><label for="ruleAction${index}">命中动作 <span class="required" aria-hidden="true">*</span></label><select id="ruleAction${index}" data-rule-field="action" data-index="${index}"><option value="">请选择关闭或复制</option>${option("pause", "关闭", rule.action === "pause")}${option("copy", "复制", rule.action === "copy")}</select></div>
        </div>
        <div class="rule-divider"></div>
        <div><div class="section-head"><div><h3>筛选条件</h3><p>指标窗口在计划与额度步骤统一设置。</p></div><button class="button button-small" type="button" data-action="add-condition" data-index="${index}">＋ 添加条件</button></div>
          <div class="conditions">${rule.conditions.length ? rule.conditions.map((condition, conditionIndex) => renderConditionRow(condition, index, conditionIndex, fields)).join("") : '<div class="condition-empty">至少添加一个条件。</div>'}</div>
        </div>
        ${rule.action === "copy" ? renderCopyParameters(rule, index) : ""}
      </div>
    </article>`;
  }

  function fieldByKey(key) { return state.meta.fields.find(field => field.key === key) || null; }

  function renderConditionRow(condition, ruleIndex, conditionIndex, fields) {
    const capability = fieldByKey(condition.field);
    const operators = capability ? capability.operators : [];
    return `<div class="condition-row">
      <div class="field"><label for="conditionField${ruleIndex}_${conditionIndex}">字段 <span class="required" aria-hidden="true">*</span></label><select id="conditionField${ruleIndex}_${conditionIndex}" data-condition-field="field" data-rule-index="${ruleIndex}" data-condition-index="${conditionIndex}"><option value="">请选择筛选字段</option>${fields.map(field => option(field.key, `${field.label} · ${field.source || "data"}`, condition.field === field.key)).join("")}</select></div>
      <div class="field"><label for="conditionOperator${ruleIndex}_${conditionIndex}">运算符 <span class="required" aria-hidden="true">*</span></label><select id="conditionOperator${ruleIndex}_${conditionIndex}" data-condition-field="operator" data-rule-index="${ruleIndex}" data-condition-index="${conditionIndex}"${capability ? "" : " disabled"}><option value="">请选择</option>${array(operators).map(value => option(value, OPERATOR_LABELS[value] || value, condition.operator === value)).join("")}</select></div>
      ${renderConditionValue(condition, capability, ruleIndex, conditionIndex)}
      <button class="icon-button" type="button" title="删除条件" aria-label="删除条件 ${conditionIndex + 1}" data-action="remove-condition" data-rule-index="${ruleIndex}" data-condition-index="${conditionIndex}">×</button>
      ${capability ? `<p class="condition-help">数据源：${h(capability.source || "—")} · 类型：${h(capability.value_type || "text")}</p>` : ""}
    </div>`;
  }

  function renderConditionValue(condition, capability, ruleIndex, conditionIndex) {
    const operator = condition.operator;
    if (!capability || !operator) return '<div class="field"><span class="field-label">值</span><input type="text" placeholder="先选择字段和运算符" disabled></div>';
    if (["exists", "not_exists"].includes(operator)) return '<div class="field"><span class="field-label">值</span><div class="locked-value"><strong>此运算符不需要填写值</strong></div></div>';
    const current = Array.isArray(condition.value) ? condition.value : [condition.value == null ? "" : condition.value];
    const spec = conditionValueSpec(capability, operator);
    if (spec.relativeDays) {
      return `<div class="field"><label for="conditionValue${ruleIndex}_${conditionIndex}">天数 <span class="required" aria-hidden="true">*</span></label><div class="input-with-unit"><input id="conditionValue${ruleIndex}_${conditionIndex}" type="number" min="1" max="3650" step="1" inputmode="numeric" placeholder="${h(spec.placeholder)}" value="${h(current[0] == null ? "" : current[0])}" data-condition-value="single" data-rule-index="${ruleIndex}" data-condition-index="${conditionIndex}"><span>天</span></div></div>`;
    }
    const inputType = spec.inputType;
    const step = spec.attributes;
    const placeholder = spec.placeholder;
    if (operator === "between") {
      return `<div class="field"><span class="field-label">范围 <span class="required" aria-hidden="true">*</span></span><div class="condition-value"><input type="${inputType}"${step} aria-label="范围起始值" placeholder="起始值" value="${h(current[0] == null ? "" : current[0])}" data-condition-value="0" data-rule-index="${ruleIndex}" data-condition-index="${conditionIndex}"><input type="${inputType}"${step} aria-label="范围结束值" placeholder="结束值" value="${h(current[1] == null ? "" : current[1])}" data-condition-value="1" data-rule-index="${ruleIndex}" data-condition-index="${conditionIndex}"></div></div>`;
    }
    let displayValue = current[0];
    if (["in", "not_in"].includes(operator) && Array.isArray(condition.value)) displayValue = condition.value.join(", ");
    return `<div class="field"><label for="conditionValue${ruleIndex}_${conditionIndex}">值 <span class="required" aria-hidden="true">*</span></label><input id="conditionValue${ruleIndex}_${conditionIndex}" type="${inputType}"${step} placeholder="${h(placeholder)}" value="${h(displayValue == null ? "" : displayValue)}" data-condition-value="single" data-rule-index="${ruleIndex}" data-condition-index="${conditionIndex}"></div>`;
  }

  function conditionValueSpec(capability, operator) {
    if (RELATIVE_DAY_OPERATORS.has(operator)) return { relativeDays: true, inputType: "number", attributes: ' min="1" max="3650" step="1" inputmode="numeric"', placeholder: "输入天数（1–3650），例如 7" };
    const valueType = text(capability && capability.value_type, "text");
    return {
      relativeDays: false,
      inputType: valueType === "number" ? "number" : valueType === "time" ? "datetime-local" : "text",
      attributes: valueType === "number" ? ' step="any" inputmode="decimal"' : "",
      placeholder: valueType === "number" ? "输入数值" : ["enum", "multi_enum"].includes(valueType) ? "输入枚举值，多个用逗号分隔" : valueType === "time" ? "选择日期时间" : ["multi_text"].includes(valueType) ? "输入多个值，用逗号分隔" : "输入匹配内容",
    };
  }

  function renderCopyParameters(rule, index) {
    const copy = rule.copy_parameters || {};
    const mode = text(copy.budget_mode);
    return `<div class="copy-options"><div><h4>复制参数</h4><p class="field-hint">正式执行会先做 Token、来源映射、预算出价和落表结构校验；复制对象先保持 PAUSED。</p></div>
      <div class="form-grid three">
        <div class="field"><label for="copyBudgetMode${index}">预算计算方式</label><select id="copyBudgetMode${index}" data-rule-copy="budget_mode" data-index="${index}"><option value="">请选择预算方式</option>${option("actual_cpi_multiplier", "X × 实际 CPI", mode === "actual_cpi_multiplier")}${option("fixed_target_cpi_multiplier", "X × 固定目标 CPI", mode === "fixed_target_cpi_multiplier")}${option("source_budget_ratio", "来源预算 × 比例", mode === "source_budget_ratio")}</select></div>
        ${mode === "actual_cpi_multiplier" ? `<div class="field"><label for="copyBudgetMultiplier${index}">CPI 倍数</label><input id="copyBudgetMultiplier${index}" type="number" step="any" min="0" inputmode="decimal" placeholder="例如 10" value="${h(copy.budget_multiplier || "")}" data-rule-copy="budget_multiplier" data-index="${index}"></div>` : ""}
        ${mode === "fixed_target_cpi_multiplier" ? `<div class="field"><label for="copyTargetCpi${index}">固定目标 CPI</label><input id="copyTargetCpi${index}" type="number" step="any" min="0" inputmode="decimal" placeholder="输入目标 CPI" value="${h(copy.target_cpi || "")}" data-rule-copy="target_cpi" data-index="${index}"></div><div class="field"><label for="copyTargetMultiplier${index}">CPI 倍数</label><input id="copyTargetMultiplier${index}" type="number" step="any" min="0" inputmode="decimal" placeholder="例如 10" value="${h(copy.budget_multiplier || "")}" data-rule-copy="budget_multiplier" data-index="${index}"></div>` : ""}
        ${mode === "source_budget_ratio" ? `<div class="field"><label for="copySourceRatio${index}">来源预算比例</label><div class="input-with-unit"><input id="copySourceRatio${index}" type="number" step="any" min="0" inputmode="decimal" placeholder="例如 50" value="${h(copy.source_budget_ratio || "")}" data-rule-copy="source_budget_ratio" data-index="${index}"><span>%</span></div></div>` : ""}
        <div class="field"><label for="copyRoasDirection${index}">ROAS 出价调整</label><select id="copyRoasDirection${index}" data-rule-copy="roas_adjustment_direction" data-index="${index}"><option value="">不设置或请选择</option>${option("increase", "提高", copy.roas_adjustment_direction === "increase")}${option("decrease", "降低", copy.roas_adjustment_direction === "decrease")}</select></div>
        ${copy.roas_adjustment_direction ? `<div class="field"><label for="copyRoasPercent${index}">调整比例</label><div class="input-with-unit"><input id="copyRoasPercent${index}" type="number" step="any" min="0" inputmode="decimal" placeholder="输入百分比" value="${h(copy.roas_adjustment_percent || "")}" data-rule-copy="roas_adjustment_percent" data-index="${index}"><span>%</span></div></div>` : ""}
        <div class="field"><label for="copyCarrier${index}">复制承载策略</label><select id="copyCarrier${index}" data-rule-copy="carrier_strategy" data-index="${index}"><option value="">请选择承载结构</option>${carrierStrategyOptions(copy.carrier_strategy)}</select><p class="field-hint">选项随 ${h(LEVEL_LABELS[state.editor.object_level] || "对象")} 层级变化。</p></div>
        <div class="field"><label for="copyCooldown${index}">来源冷却天数</label><input id="copyCooldown${index}" type="number" min="1" inputmode="numeric" placeholder="同一来源多少天内不重复" value="${h(copy.cooldown_days || "")}" data-rule-copy="cooldown_days" data-index="${index}"></div>
        <div class="field"><label for="copyDailyLimit${index}">单规则每日复制上限</label><input id="copyDailyLimit${index}" type="number" min="1" inputmode="numeric" placeholder="输入每日最多次数" value="${h(copy.daily_copy_limit || "")}" data-rule-copy="daily_copy_limit" data-index="${index}"></div>
      </div>
    </div>`;
  }

  function carrierStrategyOptions(selected) {
    const byLevel = {
      campaign: [["deep_copy_campaign", "深度复制整个 Campaign"]],
      adset: [["same_campaign", "复制到来源 Campaign"], ["new_campaign", "新建 Campaign 承载"]],
      ad: [["same_adset", "复制到来源 Ad Set"], ["isolated_adset", "新建独立 Ad Set"], ["isolated_campaign", "新建 Campaign + Ad Set"]],
    };
    return array(byLevel[state.editor.object_level]).map(([value, label]) => option(value, label, selected === value)).join("");
  }

  function renderScheduleStep() {
    const schedule = state.editor.schedule || {};
    const quotas = state.editor.quotas || {};
    const selection = state.editor.selection || {};
    return `<section class="step-pane" aria-labelledby="scheduleStepTitle">
      <div class="section-card"><div class="section-head"><div><h3 id="scheduleStepTitle">执行计划</h3><p>所有时间均按广告账户时区解释。留空的可选时间窗口不会限制执行。</p></div></div>
        <div class="section-body"><div class="form-grid three">
          <div class="field"><label for="scheduleType">执行方式</label><select id="scheduleType" data-schedule="type"><option value="">请选择执行方式</option>${option("fixed_time", "每天固定时间", schedule.type === "fixed_time")}${option("interval", "每隔 N 分钟", schedule.type === "interval")}</select></div>
          ${schedule.type === "fixed_time" ? `<div class="field"><label for="scheduleFixedTime">执行时间</label><input id="scheduleFixedTime" type="time" value="${h(schedule.fixed_time || "")}" data-schedule="fixed_time"><p class="field-hint">按每个广告账户的本地时区判断。</p></div>` : ""}
          ${schedule.type === "interval" ? `<div class="field"><label for="scheduleInterval">执行间隔</label><div class="input-with-unit"><input id="scheduleInterval" type="number" min="1" inputmode="numeric" placeholder="输入分钟数" value="${h(schedule.interval_minutes || "")}" data-schedule="interval_minutes"><span>分钟</span></div></div>` : ""}
          <div class="field"><label for="scheduleStart">允许开始时间</label><input id="scheduleStart" type="time" value="${h(schedule.allowed_start_time || "")}" data-schedule="allowed_start_time"><p class="field-hint">留空表示不限制开始时间。</p></div>
          <div class="field"><label for="scheduleEnd">允许截止时间</label><input id="scheduleEnd" type="time" value="${h(schedule.allowed_end_time || "")}" data-schedule="allowed_end_time"><p class="field-hint">早于开始时间时按跨日窗口处理。</p></div>
        </div></div>
      </div>
      <div class="section-card"><div class="section-head"><div><h3>指标窗口与候选选择</h3><p>排序始终追加对象 ID 作为稳定末位，不会因同分导致随机变化。</p></div></div>
        <div class="section-body"><div class="form-grid four">
          <div class="field"><label for="metricWindow">指标窗口</label><div class="input-with-unit"><input id="metricWindow" type="number" min="1" inputmode="numeric" placeholder="输入最近天数" value="${h(selection.metric_window_days || "")}" data-selection="metric_window_days"><span>天</span></div></div>
          <div class="field"><label for="selectionMode">候选选择</label><select id="selectionMode" data-selection="mode"><option value="">请选择候选方式</option>${option("all", "全部符合条件", selection.mode === "all")}${option("account_top_n", "每账户 Top N", selection.mode === "account_top_n")}${option("product_top_n", "每产品 Top N", selection.mode === "product_top_n")}${option("global_top_n", "全范围 Top N", selection.mode === "global_top_n")}</select></div>
          ${selection.mode && selection.mode !== "all" ? `<div class="field"><label for="selectionTopN">Top N</label><input id="selectionTopN" type="number" min="1" inputmode="numeric" placeholder="输入候选数量" value="${h(selection.top_n || "")}" data-selection="top_n"></div>` : ""}
          <div class="field"><label for="selectionSort">首要排序指标</label><select id="selectionSort" data-selection="sort_field"><option value="">请选择排序指标</option>${sortableFieldOptions(selection.sort_field)}</select></div>
          ${selection.sort_field ? `<div class="field"><label for="selectionDirection">排序方向</label><select id="selectionDirection" data-selection="sort_direction"><option value="">请选择升序或降序</option>${option("desc", "降序", selection.sort_direction === "desc")}${option("asc", "升序", selection.sort_direction === "asc")}</select></div>` : ""}
        </div></div>
      </div>
      <div class="section-card"><div class="section-head"><div><h3>执行额度</h3><p>留空表示使用服务端部署上限；页面不会预填默认次数。</p></div></div>
        <div class="section-body"><div class="form-grid three">
          <div class="field"><label for="quotaGroup">本规则组每日上限</label><input id="quotaGroup" type="number" min="1" inputmode="numeric" placeholder="输入每日最多动作数" value="${h(quotas.group_daily_limit || "")}" data-quota="group_daily_limit"></div>
          <div class="field"><label for="quotaUser">当前用户每日总上限</label><input id="quotaUser" type="number" min="1" inputmode="numeric" placeholder="输入用户每日总次数" value="${h(quotas.user_daily_limit || "")}" data-quota="user_daily_limit"></div>
          <div class="field"><label for="quotaCooldown">同一对象冷却期</label><div class="input-with-unit"><input id="quotaCooldown" type="number" min="1" inputmode="numeric" placeholder="输入冷却天数" value="${h(quotas.object_cooldown_days || "")}" data-quota="object_cooldown_days"><span>天</span></div></div>
        </div></div>
      </div>
    </section>`;
  }

  function sortableFieldOptions(selected) {
    return fieldsForLevel(false).filter(field => field.value_type === "number").map(field => option(field.key, field.label, selected === field.key)).join("");
  }

  function selectionValidationErrors(selection) {
    const candidate = selection || {};
    const errors = [];
    if (!text(candidate.mode)) {
      errors.push("请选择候选选择方式");
      return errors;
    }
    if (candidate.mode !== "all") {
      if (!(Number(candidate.top_n) > 0)) errors.push("Top N 候选方式需要填写大于 0 的数量");
      if (!text(candidate.sort_field)) errors.push("Top N 候选方式需要选择排序指标");
      if (!["asc", "desc"].includes(candidate.sort_direction)) errors.push("Top N 候选方式需要选择排序方向");
    }
    return errors;
  }

  function validateEditor() {
    const editor = state.editor;
    const errors = [];
    if (!text(editor.name)) errors.push("请填写规则组名称");
    if (editor.channel !== "facebook") errors.push("请选择 Facebook 渠道");
    if (!text(editor.optimizer_id)) errors.push("请选择或解析优化师");
    if (!array(editor.products).length) errors.push("请至少选择一个短剧产品");
    if (!LEVEL_LABELS[editor.object_level]) errors.push("请选择调控对象层级");
    if (!(Number(editor.selection.metric_window_days) > 0)) errors.push("请填写大于 0 的指标窗口天数");
    if (!array(editor.rules).length) errors.push("请至少添加一条筛选规则");
    array(editor.rules).forEach((rule, ruleIndex) => {
      if (!['and', 'or'].includes(rule.logic)) errors.push(`规则 ${ruleIndex + 1}：请选择条件关系`);
      if (!['pause', 'copy'].includes(rule.action)) errors.push(`规则 ${ruleIndex + 1}：请选择命中动作`);
      if (!array(rule.conditions).length) errors.push(`规则 ${ruleIndex + 1}：至少添加一个条件`);
      array(rule.conditions).forEach((condition, conditionIndex) => {
        const capability = fieldByKey(condition.field);
        if (!capability || !fieldsForLevel(false).some(field => field.key === condition.field)) errors.push(`规则 ${ruleIndex + 1} 条件 ${conditionIndex + 1}：请选择可用字段`);
        if (!condition.operator || !array(capability && capability.operators).includes(condition.operator)) errors.push(`规则 ${ruleIndex + 1} 条件 ${conditionIndex + 1}：请选择运算符`);
        if (!["exists", "not_exists"].includes(condition.operator)) {
          const values = Array.isArray(condition.value) ? condition.value : [condition.value];
          const requiredCount = condition.operator === "between" ? 2 : 1;
          const incomplete = values.slice(0, requiredCount).some(value => String(value == null ? "" : value).trim() === "") || values.length < requiredCount;
          if (incomplete) errors.push(`规则 ${ruleIndex + 1} 条件 ${conditionIndex + 1}：请填写完整值`);
          else if (RELATIVE_DAY_OPERATORS.has(condition.operator) && (!(Number(condition.value) >= 1) || Number(condition.value) > 3650 || !Number.isInteger(Number(condition.value)))) errors.push(`规则 ${ruleIndex + 1} 条件 ${conditionIndex + 1}：请填写 1 到 3650 的整数天数`);
        }
      });
      if (rule.action === "copy") {
        const copy = rule.copy_parameters || {};
        if (!text(copy.carrier_strategy)) errors.push(`规则 ${ruleIndex + 1}：请选择复制承载结构`);
        if (!text(copy.budget_mode)) errors.push(`规则 ${ruleIndex + 1}：请选择复制预算方式`);
        if (["actual_cpi_multiplier", "fixed_target_cpi_multiplier"].includes(copy.budget_mode) && !(Number(copy.budget_multiplier) > 0)) errors.push(`规则 ${ruleIndex + 1}：请填写大于 0 的 CPI 倍数`);
        if (copy.budget_mode === "fixed_target_cpi_multiplier" && !(Number(copy.target_cpi) > 0)) errors.push(`规则 ${ruleIndex + 1}：请填写大于 0 的固定目标 CPI`);
        if (copy.budget_mode === "source_budget_ratio" && !(Number(copy.source_budget_ratio) > 0)) errors.push(`规则 ${ruleIndex + 1}：请填写大于 0 的来源预算比例`);
        const hasRoasDirection = Boolean(copy.roas_adjustment_direction);
        const hasRoasPercent = String(copy.roas_adjustment_percent || "").trim() !== "";
        if (hasRoasDirection !== hasRoasPercent) errors.push(`规则 ${ruleIndex + 1}：ROAS 调整方向与比例需同时设置`);
        if (hasRoasPercent && (!(Number(copy.roas_adjustment_percent) > 0) || Number(copy.roas_adjustment_percent) > 100)) errors.push(`规则 ${ruleIndex + 1}：ROAS 调整比例需在 0 到 100 之间`);
      }
    });
    if (editor.schedule.type === "fixed_time" && !editor.schedule.fixed_time) errors.push("固定时间计划需要填写执行时间");
    if (editor.schedule.type === "interval" && !editor.schedule.interval_minutes) errors.push("间隔计划需要填写分钟数");
    errors.push(...selectionValidationErrors(editor.selection));
    return errors;
  }

  function renderReviewStep() {
    const editor = state.editor;
    const errors = validateEditor();
    const optimizer = optimizerLabel(editor.optimizer_id);
    const actionLabels = editor.rules.map(rule => ACTION_LABELS[rule.action] || "未选择");
    return `<section class="step-pane" aria-labelledby="reviewStepTitle">
      <div class="section-card"><div class="section-head"><div><h3 id="reviewStepTitle">配置总览</h3><p>保存前检查所有显式选择；placeholder 从不进入请求数据。</p></div><span class="pill ${errors.length ? "pill-warning" : "pill-success"}">${errors.length ? `${errors.length} 项待完善` : "配置完整"}</span></div>
        <div class="section-body"><div class="review-grid">
          <div class="review-card"><h4>范围</h4><div class="review-list">
            ${reviewRow("渠道", editor.channel === "facebook" ? "Facebook" : "未选择")}${reviewRow("优化师", optimizer)}${reviewRow("产品", editor.products.length ? editor.products.map(productLabel).join("、") : "未选择")}${reviewRow("账户时区", editor.account_timezones.length ? editor.account_timezones.join("、") : "不限制")}
          </div></div>
          <div class="review-card"><h4>策略</h4><div class="review-list">
            ${reviewRow("调控对象", LEVEL_LABELS[editor.object_level] || "未选择")}${reviewRow("运行模式", MODE_LABELS[editor.run_mode] || "未选择")}${reviewRow("规则数量", String(editor.rules.length))}${reviewRow("命中动作", actionLabels.length ? actionLabels.join("、") : "未配置")}
          </div></div>
          <div class="review-card"><h4>计划与选择</h4><div class="review-list">
            ${reviewRow("执行方式", scheduleLabel(editor.schedule))}${reviewRow("允许窗口", windowLabel(editor.schedule))}${reviewRow("指标窗口", editor.selection.metric_window_days ? `最近 ${editor.selection.metric_window_days} 天` : "未设置")}${reviewRow("候选选择", selectionLabel(editor.selection))}
          </div></div>
          <div class="review-card"><h4>范围估算</h4>${state.estimate ? renderEstimateMetrics() : '<p class="field-hint">尚未估算。你可以返回“对象与模式”步骤估算，也可以保存后直接试算。</p>'}</div>
        </div></div>
      </div>
      <div class="section-card"><div class="section-head"><div><h3>安全检查</h3><p>服务端仍会执行同样的权限、能力和版本校验。</p></div></div><div class="section-body"><div class="checklist">
        ${checkItem(!errors.length, "必填配置", errors.length ? errors.join("；") : "名称、范围、层级、规则和条件完整")}
        ${checkItem(true, "新建安全状态", "保存接口强制停用，并将新规则模式固定为只观察")}
        ${checkItem(editor.channel === "facebook", "渠道能力", editor.channel === "facebook" ? "本期 Facebook 可试算" : "TikTok 或未选择渠道不可用")}
        ${checkItem(state.meta.permissions.canEnable, "持续自动扫描", state.meta.permissions.canEnable ? "计划调度器可用，启用仍受服务端权限与安全开关约束" : "计划调度器尚未发布；当前仅支持保存草稿和手动试算")}
        ${checkItem(editor.run_mode === "observe", "外部写风险", editor.run_mode === "observe" ? "观察模式不调用 Meta 写接口" : "正式执行仍需有效试算、二次确认与总开关")}
        ${checkItem(!editor.rules.some(rule => rule.action === "copy") || editor.run_mode === "observe", "复制边界", editor.rules.some(rule => rule.action === "copy") ? "复制可观察；正式复制会在 Token 前失败关闭" : "当前没有复制动作")}
      </div></div></div>
      <div class="section-card"><div class="section-head"><div><h3>立即试算</h3><p>试算会创建不可变快照和执行日志，但不会更改 Meta 对象。</p></div></div><div class="section-body"><div class="estimate-card"><div><h4>${editor.id ? "保存后可立即运行试算" : "需要先保存草稿"}</h4><p>“保存并立即试算”会先保存当前配置，再使用服务端返回的最新版本发起试算。</p></div><span class="pill pill-safe">零 Meta 写入</span></div></div></div>
    </section>`;
  }

  function reviewRow(label, value) { return `<div class="review-row"><span>${h(label)}</span><strong>${h(value)}</strong></div>`; }
  function checkItem(ok, title, description) {
    const unknown = ok == null;
    return `<div class="check-item${unknown ? " is-unknown" : (ok ? "" : " is-missing")}"><span class="check-mark" aria-hidden="true">${unknown ? "?" : (ok ? "✓" : "!")}</span><span><strong>${h(title)}</strong><small>${h(description)}</small></span></div>`;
  }
  function scheduleLabel(schedule) {
    if (schedule.type === "fixed_time") return schedule.fixed_time ? `每天 ${schedule.fixed_time}` : "每天固定时间（未填写）";
    if (schedule.type === "interval") return schedule.interval_minutes ? `每隔 ${schedule.interval_minutes} 分钟` : "间隔执行（未填写）";
    return "未设置";
  }
  function windowLabel(schedule) { return schedule.allowed_start_time || schedule.allowed_end_time ? `${schedule.allowed_start_time || "不限"} 至 ${schedule.allowed_end_time || "不限"}` : "不限制"; }
  function selectionLabel(selection) {
    const labels = { all: "全部符合条件", account_top_n: "每账户 Top N", product_top_n: "每产品 Top N", global_top_n: "全范围 Top N" };
    if (!selection.mode) return "未设置";
    return `${labels[selection.mode] || selection.mode}${selection.top_n ? ` · N=${selection.top_n}` : ""}${selection.sort_field ? ` · ${selection.sort_field} ${selection.sort_direction || ""}` : ""}`;
  }

  function nonBlankObject(source) {
    const result = {};
    Object.entries(source || {}).forEach(([key, value]) => {
      if (value === "" || value == null) return;
      result[key] = value;
    });
    return result;
  }

  function numericIfPossible(value) {
    if (value === "" || value == null) return value;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : value;
  }

  function conditionPayload(condition) {
    const result = { field: condition.field, operator: condition.operator };
    if (["exists", "not_exists"].includes(condition.operator)) return result;
    const capability = fieldByKey(condition.field);
    if (condition.operator === "between") {
      result.value = array(condition.value).slice(0, 2).map(value => capability && capability.value_type === "number" ? numericIfPossible(value) : String(value));
    } else if (["in", "not_in"].includes(condition.operator)) {
      const values = Array.isArray(condition.value) ? condition.value : String(condition.value || "").split(",");
      result.value = values.map(item => String(item).trim()).filter(Boolean);
    } else {
      result.value = RELATIVE_DAY_OPERATORS.has(condition.operator) || (capability && capability.value_type === "number") ? numericIfPossible(condition.value) : condition.value;
    }
    return result;
  }

  function editorPayload() {
    const editor = state.editor;
    const payload = {
      name: text(editor.name), description: text(editor.description), channel: editor.channel,
      object_level: editor.object_level, run_mode: editor.run_mode, optimizer_id: numericIfPossible(editor.optimizer_id),
      products: editor.products.slice(), account_timezones: editor.account_timezones.slice(),
      rules: editor.rules.map((rule, index) => {
        const item = {
          rule_id: rule.rule_id, name: text(rule.name), logic: rule.logic, action: rule.action,
          conditions: rule.conditions.map(conditionPayload),
        };
        if (rule.priority !== "" && rule.priority != null) item.priority = numericIfPossible(rule.priority);
        else item.priority = index;
        if (rule.action === "copy") {
          item.copy_parameters = nonBlankObject(Object.fromEntries(Object.entries(rule.copy_parameters || {}).map(([key, value]) => [key, numericCopyField(key) ? numericIfPossible(value) : value])));
        }
        return item;
      }),
      schedule: nonBlankObject(Object.fromEntries(Object.entries(editor.schedule || {}).map(([key, value]) => [key, key === "interval_minutes" ? numericIfPossible(value) : value]))),
      quotas: nonBlankObject(Object.fromEntries(Object.entries(editor.quotas || {}).map(([key, value]) => [key, numericIfPossible(value)]))),
      selection: nonBlankObject(Object.fromEntries(Object.entries(editor.selection || {}).map(([key, value]) => [key, ["top_n", "metric_window_days"].includes(key) ? numericIfPossible(value) : value]))),
    };
    return payload;
  }

  function numericCopyField(key) {
    return ["budget_multiplier", "target_cpi", "source_budget_ratio", "roas_adjustment_percent", "cooldown_days", "daily_copy_limit"].includes(key);
  }

  async function saveEditor(options) {
    const settings = Object.assign({ preview: false }, options || {});
    if (state.editor && state.editor.id && !canMutateGroup(state.editor, state.actor)) {
      toast("该规则组不属于当前用户，只能查看，不能保存修改。", "error");
      return null;
    }
    const errors = validateEditor();
    if (errors.length) {
      state.editorStep = firstInvalidStep(errors);
      renderEditor();
      toast(errors[0], "error");
      return null;
    }
    if (!beginInFlight("editor-save")) return null;
    const payload = editorPayload();
    renderEditor();
    try {
      const wasUpdate = Boolean(state.editor.id);
      const previousOwnerId = text(state.editor.owner_user_id, text(state.actor && state.actor.id));
      const request = saveRequestForEditor(state.editor, payload);
      const result = await api(request.path, {
        method: request.method, headers: request.headers, body: request.body,
      });
      state.editor = normalizeGroupForEditor(result);
      state.editor.owner_user_id = text(state.editor.owner_user_id, previousOwnerId);
      state.editor.can_mutate = true;
      state.editorDirty = false;
      invalidateEstimate();
      toast(wasUpdate ? "规则组已保存，行为变化会使旧试算失效。" : "规则组已保存为停用 + 只观察。", "success");
      if (settings.preview) await previewGroup(state.editor.id, true);
      else renderEditor();
      return result;
    } catch (error) {
      toast(errorMessage(error), "error");
      return null;
    } finally {
      endInFlight("editor-save");
      if (state.editor) renderEditor();
    }
  }

  function saveRequestForEditor(editor, payload) {
    const isUpdate = Boolean(editor && editor.id);
    const headers = {};
    if (isUpdate && editor.version) headers["If-Match"] = String(editor.version);
    return {
      path: isUpdate ? `/rule-groups/${encodeURIComponent(editor.id)}` : "/rule-groups",
      method: isUpdate ? "PUT" : "POST",
      headers,
      body: JSON.stringify(payload || {}),
    };
  }

  function previewPath(groupId) {
    return `/rule-groups/${encodeURIComponent(groupId)}/preview`;
  }

  function firstInvalidStep(errors) {
    const joined = errors.join(" ");
    if (/名称|渠道|优化师|产品/.test(joined)) return 1;
    if (/对象/.test(joined)) return 2;
    if (/规则|条件|字段|运算符|值|动作/.test(joined)) return 3;
    if (/时间|间隔|候选选择|Top N|排序|指标窗口/.test(joined)) return 4;
    return 5;
  }

  async function estimateScope() {
    if (!state.editor) return;
    if (state.editor.channel !== "facebook" || !state.editor.optimizer_id || !state.editor.products.length || !state.editor.object_level || !(Number(state.editor.selection.metric_window_days) > 0)) {
      toast("请先选择 Facebook、优化师、短剧产品、调控对象，并填写指标窗口。", "error");
      return;
    }
    const requestSerial = ++state.estimateRequestSerial;
    const fingerprint = scopeFingerprint(state.editor);
    state.estimateLoading = true;
    renderEditor();
    try {
      const result = await api("/scope-estimate", {
        method: "POST",
        body: JSON.stringify({
          channel: state.editor.channel,
          optimizer_id: numericIfPossible(state.editor.optimizer_id),
          products: state.editor.products.slice(),
          account_timezones: state.editor.account_timezones.slice(),
          object_level: state.editor.object_level,
          metric_window_days: numericIfPossible(state.editor.selection.metric_window_days),
        }),
      });
      if (requestSerial !== state.estimateRequestSerial || fingerprint !== scopeFingerprint(state.editor)) return;
      state.estimate = result;
      toast("范围估算完成。", "success");
    } catch (error) {
      if (requestSerial !== state.estimateRequestSerial || fingerprint !== scopeFingerprint(state.editor)) return;
      state.estimate = null;
      toast(errorMessage(error), "error");
    } finally {
      if (requestSerial === state.estimateRequestSerial) {
        state.estimateLoading = false;
        renderEditor();
      }
    }
  }

  async function previewGroup(groupId, fromEditor) {
    if (!groupId) return;
    const group = listedGroup(groupId);
    if (!fromEditor && group && !canMutateGroup(group, state.actor)) {
      toast("该规则组不属于当前用户，只能查看，不能发起试算。", "error");
      return;
    }
    const flightKey = `preview:${groupId}`;
    if (!beginInFlight(flightKey)) return;
    if (!fromEditor) renderRuleGroupShell();
    try {
      const result = await api(previewPath(groupId), { method: "POST", body: "{}" });
      const summary = result.summary || result;
      const targetCount = summary.target_count != null ? summary.target_count : summary.matched_count;
      toast(targetCount == null ? "试算已完成，可在执行日志查看详情。" : `试算完成，命中 ${formatCount(targetCount)} 个对象。`, "success");
      if (fromEditor) {
        invalidateEstimate();
        state.editor = null;
        state.editorStep = 1;
        state.editorDirty = false;
        state.openMulti = "";
        state.openSingle = "";
        renderRuleGroupShell();
        await loadRuleGroups();
      } else await loadRuleGroups();
    } catch (error) { toast(errorMessage(error), "error"); }
    finally {
      endInFlight(flightKey);
      if (!fromEditor && state.editor == null) renderRuleGroupShell();
    }
  }

  async function executeGroup(groupId) {
    if (!groupId) return;
    const group = listedGroup(groupId);
    if (group && !canMutateGroup(group, state.actor)) {
      toast("该规则组不属于当前用户，只能查看，不能正式执行。", "error");
      return;
    }
    const flightKey = `execute:${groupId}`;
    if (!beginInFlight(flightKey)) return;
    renderRuleGroupShell();
    try {
      const confirmed = await confirmDialog({
        title: "确认真实执行这个规则组？",
        message: "系统会按最新试算结果真实暂停或复制 Meta 对象。复制对象先以 PAUSED 创建，落表和关联校验通过后才按服务端激活开关处理。请先确认试算命中对象无误。",
        confirmLabel: "确认执行",
        danger: true,
        phrase: "EXECUTE_LIVE_RULE_GROUP",
      });
      if (!confirmed) return;
      const result = await api(`/rule-groups/${encodeURIComponent(groupId)}/execute`, {
        method: "POST",
        body: JSON.stringify({ confirm: confirmed }),
      });
      const summary = result.summary || {};
      toast(`正式执行完成：成功 ${formatCount(summary.succeeded_count || 0)}，跳过 ${formatCount(summary.skipped_count || 0)}，失败 ${formatCount(summary.failed_count || 0)}。`, summary.failed_count ? "error" : "success");
      await loadRuleGroups();
    } catch (error) {
      toast(errorMessage(error), "error");
    } finally {
      endInFlight(flightKey);
      if (!state.editor) renderRuleGroupShell();
    }
  }

  function setNestedValue(target, key, value) {
    target[key] = value;
    state.editorDirty = true;
  }

  function renderLogShell() {
    const root = document.getElementById("executionLogsApp");
    if (!root) return;
    const optimizerFilter = state.actor && state.actor.isAdmin ? `<div class="field"><span class="field-label">优化师</span>${renderSearchableSingle("log-optimizer", "全部优化师", state.meta.optimizers.map(item => ({ value: item.id, label: item.name, description: item.email })), String(state.logFilters.optimizer_id || ""), true)}</div>` : "";
    root.innerHTML = `
      <div class="toolbar"><div class="toolbar-copy"><h2>V3 事件审计</h2><p>列表使用服务端分页；对象详情在需要时单独读取。</p></div><div class="toolbar-actions"><button class="button" type="button" data-action="reload-logs"${state.logs.loading ? " disabled" : ""}>刷新日志</button></div></div>
      ${renderLogMetrics()}
      <section class="filter-bar logs" aria-label="执行日志筛选">
        <div class="field"><label for="logDateFrom">开始日期</label><input id="logDateFrom" type="date" data-log-filter="date_from" value="${h(state.logFilters.date_from || "")}"></div>
        <div class="field"><label for="logDateTo">结束日期</label><input id="logDateTo" type="date" data-log-filter="date_to" value="${h(state.logFilters.date_to || "")}"></div>
        <div class="field"><span class="field-label">产品</span>${renderSearchableSingle("log-product", "全部短剧产品", state.meta.products.map(item => ({ value: item.value, label: item.label, description: item.description, disabled: !item.enabled })), String(state.logFilters.product || ""), true)}</div>
        ${optimizerFilter}
        <div class="field"><label for="logLevel">调控对象</label><select id="logLevel" data-log-filter="object_level"><option value="">全部层级</option>${Object.entries(LEVEL_LABELS).map(([value, label]) => option(value, label, state.logFilters.object_level === value)).join("")}</select></div>
        <div class="field"><label for="logAction">命中动作</label><select id="logAction" data-log-filter="action"><option value="">全部动作</option>${option("pause", "关闭", state.logFilters.action === "pause")}${option("copy", "复制", state.logFilters.action === "copy")}</select></div>
        <div class="field"><label for="logMode">运行模式</label><select id="logMode" data-log-filter="run_mode"><option value="">全部模式</option>${option("observe", "只观察", state.logFilters.run_mode === "observe")}${option("live", "正式执行", state.logFilters.run_mode === "live")}</select></div>
        <div class="field"><label for="logStatus">状态</label><select id="logStatus" data-log-filter="status"><option value="">全部状态</option>${["pending", "running", "observed", "completed", "partial", "blocked", "failed", "skipped"].map(value => option(value, STATUS_LABELS[value], state.logFilters.status === value)).join("")}</select></div>
        <div class="field"><label for="logTrigger">触发来源</label><select id="logTrigger" data-log-filter="trigger_source"><option value="">全部来源</option>${option("manual_preview", "手动试算", state.logFilters.trigger_source === "manual_preview")}${option("schedule", "计划调度", state.logFilters.trigger_source === "schedule")}${option("manual_execute", "手动执行", state.logFilters.trigger_source === "manual_execute")}</select></div>
        <div class="field"><label for="logGroupId">规则组 / 日志 ID</label><input id="logGroupId" type="search" autocomplete="off" placeholder="输入规则组或日志 ID" value="${h(state.logFilters.keyword || "")}" data-log-filter="keyword"></div>
        <div class="field"><label for="logObjectId">对象 ID</label><input id="logObjectId" type="search" autocomplete="off" placeholder="输入 Campaign、Ad Set 或 Ad ID" value="${h(state.logFilters.object_id || "")}" data-log-filter="object_id"></div>
        <div class="filter-actions"><button class="button button-small" type="button" data-action="reset-log-filters">清空</button><button class="button button-small" type="button" data-action="apply-log-filters">查询</button></div>
      </section>
      <section class="panel table-panel" aria-labelledby="logListTitle"><div class="panel-header"><div><h2 id="logListTitle">事件与批次</h2><p>${state.logs.loading ? "正在读取…" : `共 ${formatCount(state.logs.total)} 条记录`}</p></div><span class="pill">服务端分页</span></div><div aria-live="polite">${renderLogTable()}</div></section>
      ${state.detail || state.detailLoading ? renderExecutionDetail() : ""}`;
  }

  function renderLogMetrics() {
    const summary = state.logs.summary || {};
    const metrics = [
      ["事件总数", summary.total != null ? summary.total : state.logs.total, "当前筛选"],
      ["命中对象", summary.target_count != null ? summary.target_count : summary.requested_count, "含观察命中"],
      ["成功", summary.success_count, "完成动作"], ["跳过", summary.skipped_count, "终态跳过"], ["异常 / 阻断", summary.error_count != null ? summary.error_count : summary.blocked_count, "需关注"],
    ];
    return `<section class="metric-cards" aria-label="执行概览">${metrics.map(([label, value, note]) => `<div class="metric-card"><span>${h(label)}</span><strong>${h(value == null ? "—" : formatCount(value))}</strong><small>${h(note)}</small></div>`).join("")}</section>`;
  }

  function renderLogTable() {
    if (state.logs.loading) {
      return `<div class="table-scroll"><table aria-label="日志加载中"><thead><tr><th>时间与事件</th><th>规则组</th><th>范围</th><th>对象 / 动作</th><th>结果</th><th>状态</th><th></th></tr></thead><tbody>${Array.from({ length: 7 }, () => '<tr><td><span class="skeleton">2026-00-00</span></td><td><span class="skeleton">规则组</span></td><td><span class="skeleton">范围</span></td><td><span class="skeleton">对象</span></td><td><span class="skeleton">结果</span></td><td><span class="skeleton">状态</span></td><td></td></tr>').join("")}</tbody></table></div>`;
    }
    if (!state.logs.items.length) return '<div class="empty-state"><div><span class="empty-icon" aria-hidden="true">志</span><h3>当前筛选没有执行记录</h3><p>试算、计划调度和正式执行会在这里留下审计记录。</p><button class="button" type="button" data-action="reset-log-filters">清空筛选</button></div></div>';
    const rows = state.logs.items.map(item => {
      const id = executionIdOf(item);
      const status = text(executionValue(item, ["status", "execution_status"]), "pending").toLowerCase();
      const products = array(item.products || item.product_values);
      const actions = array(item.actions).length ? item.actions : [item.action].filter(Boolean);
      const targetCount = executionValue(item, ["target_count", "requested_count", "matched_count"]);
      const success = executionValue(item, ["success_count", "completed_count"]);
      const skipped = executionValue(item, ["skipped_count"]);
      const failed = executionValue(item, ["error_count", "failed_count"]);
      return `<tr>
        <td><div class="cell-title"><strong>${h(prettyDate(item.started_at || item.created_at || item.business_date))}</strong><small class="log-id">${h(id)}</small></div></td>
        <td><div class="cell-title"><strong>${h(text(item.rule_group_name, "未命名规则组"))}</strong><small>${h(text(item.rule_group_id))} · ${h(triggerLabel(item.trigger_source || item.trigger))}</small></div></td>
        <td><div class="cell-stack"><div class="chip-row">${products.slice(0, 2).map(product => `<span class="chip" title="${h(product)}">${h(productLabel(product))}</span>`).join("")}${products.length > 2 ? `<span class="chip">+${products.length - 2}</span>` : ""}</div><small class="cell-muted">${h(optimizerLabel(item.optimizer_id, item.optimizer_name))}</small></div></td>
        <td><div class="cell-stack"><span class="pill pill-info">${h(LEVEL_LABELS[item.object_level] || item.object_level || "—")}</span><small class="cell-muted">${h(actions.map(value => ACTION_LABELS[value] || value).join(" / ") || "—")} · ${h(MODE_LABELS[item.run_mode] || item.run_mode || "—")}</small></div></td>
        <td><div class="cell-stack"><span>命中 ${h(displayCount(targetCount))}</span><small class="cell-muted">成功 ${h(displayCount(success))} · 跳过 ${h(displayCount(skipped))} · 异常 ${h(displayCount(failed))}</small></div></td>
        <td><span class="status-line"><span class="status-dot ${statusClass(status)}" aria-hidden="true"></span><strong>${h(STATUS_LABELS[status] || status)}</strong></span></td>
        <td><button class="button button-small" type="button" data-action="open-log-detail" data-id="${h(id)}">查看详情</button></td>
      </tr>`;
    }).join("");
    const start = state.logs.total ? (state.logs.page - 1) * state.logs.pageSize + 1 : 0;
    const end = Math.min(state.logs.page * state.logs.pageSize, state.logs.total);
    const pageCount = Math.max(1, Math.ceil(state.logs.total / state.logs.pageSize));
    return `<div class="table-scroll"><table aria-label="执行日志列表"><thead><tr><th>时间与事件</th><th>规则组</th><th>产品与优化师</th><th>对象 / 动作</th><th>结果</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table></div><div class="pagination"><p>显示 ${start}–${end} / ${formatCount(state.logs.total)}</p><div class="pagination-controls"><button class="button button-small" type="button" data-action="log-page-prev"${state.logs.page <= 1 ? " disabled" : ""}>上一页</button><span class="pill">${state.logs.page} / ${pageCount}</span><button class="button button-small" type="button" data-action="log-page-next"${state.logs.page >= pageCount ? " disabled" : ""}>下一页</button></div></div>`;
  }

  function triggerLabel(value) {
    return { manual_preview: "手动试算", schedule: "计划调度", manual_execute: "手动执行", runner: "计划调度" }[value] || text(value, "未知来源");
  }

  function executionValue(item, keys) {
    const source = item || {};
    const summary = source.summary && typeof source.summary === "object" ? source.summary : {};
    for (const key of array(keys)) {
      if (Object.prototype.hasOwnProperty.call(source, key) && source[key] != null) return source[key];
      if (Object.prototype.hasOwnProperty.call(summary, key) && summary[key] != null) return summary[key];
    }
    return null;
  }

  function displayCount(value) {
    return value == null || value === "" ? "—" : formatCount(value);
  }

  async function loadExecutions() {
    const serial = ++state.requestSerial;
    state.logs.loading = true;
    renderLogShell();
    try {
      const payload = await api(`/executions?${queryString(state.logFilters, state.logs.page, state.logs.pageSize)}`);
      if (serial !== state.requestSerial) return;
      const normalized = normalizeList(payload, ["items", "executions", "rows"]);
      Object.assign(state.logs, normalized, { loading: false });
    } catch (error) {
      if (serial !== state.requestSerial) return;
      state.logs.loading = false;
      toast(errorMessage(error), "error");
    }
    renderLogShell();
  }

  async function openExecutionDetail(id) {
    if (!id) return;
    state.detailLoading = true; state.detail = null; renderLogShell();
    try {
      const payload = await api(`/executions/${encodeURIComponent(id)}`);
      state.detail = payload.execution || payload;
    } catch (error) { toast(errorMessage(error), "error"); }
    state.detailLoading = false; renderLogShell();
    window.requestAnimationFrame(() => document.querySelector(".detail-drawer")?.focus());
  }

  function closeDetail() { state.detail = null; state.detailLoading = false; renderLogShell(); }

  function renderExecutionDetail() {
    if (state.detailLoading) return '<div class="detail-overlay" data-action="close-detail"><aside class="detail-drawer" tabindex="-1" aria-label="日志详情加载中"><div class="detail-head"><div><h2>正在读取详情</h2><p>对象明细按需加载</p></div><button class="icon-button" type="button" data-action="close-detail" aria-label="关闭">×</button></div><div class="state-panel"><span class="spinner" aria-hidden="true"></span><div><strong>正在加载</strong><p>请稍候</p></div></div></aside></div>';
    const item = state.detail || {};
    const id = executionIdOf(item);
    const stages = normalizeStages(item);
    const reasons = normalizeReasons(item);
    const targets = array(item.targets || item.execution_targets || item.target_samples).slice(0, 100);
    const snapshotValid = executionValue(item, ["snapshot_valid"]);
    const metaWriteCount = executionValue(item, ["meta_write_count"]);
    const snapshotState = snapshotValid == null ? null : snapshotValid === true;
    const writeState = metaWriteCount == null ? null : (item.run_mode !== "observe" || Number(metaWriteCount) === 0);
    return `<div class="detail-overlay" data-action="close-detail"><aside class="detail-drawer" tabindex="-1" role="dialog" aria-modal="true" aria-labelledby="detailTitle">
      <div class="detail-head"><div><h2 id="detailTitle">${h(text(item.rule_group_name, "执行详情"))}</h2><p class="log-id">${h(id)}</p></div><button class="icon-button" type="button" data-action="close-detail" aria-label="关闭详情">×</button></div>
      <div class="detail-body">
        <section class="detail-section"><h3>概要</h3><div class="review-card"><div class="review-list">${reviewRow("状态", STATUS_LABELS[item.status] || item.status || "—")}${reviewRow("触发来源", triggerLabel(item.trigger_source || item.trigger))}${reviewRow("产品", array(item.products).join("、") || "—")}${reviewRow("优化师", optimizerLabel(item.optimizer_id, item.optimizer_name))}${reviewRow("调控对象", LEVEL_LABELS[item.object_level] || item.object_level || "—")}${reviewRow("运行模式", MODE_LABELS[item.run_mode] || item.run_mode || "—")}${reviewRow("配置版本", text(item.config_version || item.rule_group_version, "—"))}${reviewRow("行为校验", text(item.behavior_hash || item.config_hash, "—"))}</div></div></section>
        <section class="detail-section"><h3>阶段时间线</h3>${stages.length ? `<div class="timeline">${stages.map(stage => `<div class="timeline-item"><span class="timeline-dot" aria-hidden="true"></span><div class="timeline-copy"><strong>${h(stage.label)}</strong><span>${h(prettyDate(stage.at))}${stage.status ? ` · ${h(STATUS_LABELS[stage.status] || stage.status)}` : ""}</span></div></div>`).join("")}</div>` : '<div class="condition-empty">暂无阶段时间线。</div>'}</section>
        <section class="detail-section"><h3>原因汇总</h3>${reasons.length ? `<div class="reason-list">${reasons.map(reason => `<div class="reason-item"><span>${h(reason.reason)}</span><strong>${h(formatCount(reason.count))}</strong></div>`).join("")}</div>` : '<div class="condition-empty">没有跳过、阻断或失败原因。</div>'}</section>
        <section class="detail-section"><h3>对象明细${targets.length ? `（最多展示 ${targets.length} 条）` : ""}</h3>${renderTargetTable(targets)}</section>
        <section class="detail-section"><h3>数据完整性</h3><div class="checklist">${checkItem(snapshotState, "快照校验", snapshotValid == null ? "未校验" : (snapshotValid === false ? "快照摘要与明细不一致，事件已隔离" : "快照存在且校验通过"))}${checkItem(writeState, "外部写约束", item.run_mode === "observe" ? `观察模式 Meta 写入 ${h(displayCount(metaWriteCount))} 次` : `Meta 写入 ${h(displayCount(metaWriteCount))} 次`)}</div></section>
      </div>
    </aside></div>`;
  }

  function normalizeStages(item) {
    const explicit = array(item.stages || item.timeline).map(stage => ({ label: text(stage.label || stage.name || stage.stage), at: stage.at || stage.created_at || stage.timestamp, status: text(stage.status) })).filter(stage => stage.label);
    if (explicit.length) return explicit;
    return [["事件创建", item.created_at, "pending"], ["开始扫描", item.started_at, "running"], ["完成", item.completed_at || item.finished_at, item.status]].filter(stage => stage[1]).map(stage => ({ label: stage[0], at: stage[1], status: stage[2] }));
  }

  function normalizeReasons(item) {
    const summary = item && item.summary && typeof item.summary === "object" ? item.summary : {};
    const raw = item.reason_counts || item.reasons || item.skip_reasons || summary.reason_counts || summary.reasons || summary.skip_reasons || {};
    if (Array.isArray(raw)) return raw.map(value => ({ reason: text(value.reason || value.code || value.name), count: asNumber(value.count, 0) })).filter(value => value.reason);
    return Object.entries(raw || {}).map(([reason, count]) => ({ reason, count: asNumber(count, 0) }));
  }

  function renderTargetTable(targets) {
    if (!targets.length) return '<div class="condition-empty">当前详情没有可展示的对象样本。</div>';
    return `<div class="table-scroll"><table aria-label="执行对象明细"><thead><tr><th>对象 ID</th><th>产品</th><th>动作</th><th>结果</th><th>原因</th></tr></thead><tbody>${targets.map(target => `<tr><td class="log-id">${h(text(target.object_id || target.target_id))}</td><td title="${h(text(target.product))}">${h(productLabel(target.product))}</td><td>${h(ACTION_LABELS[target.action] || target.action || "—")}</td><td>${h(STATUS_LABELS[target.status] || target.status || target.result || "—")}</td><td>${h(text(target.reason || target.error_code || target.skip_reason, "—"))}</td></tr>`).join("")}</tbody></table></div>`;
  }

  let filterTimer = 0;
  let dialogResolver = null;

  function handleInput(event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.matches("[data-multi-search], [data-single-search]")) {
      const query = String(target.value || "").trim().toLowerCase();
      target.closest(".multi-menu")?.querySelectorAll("[data-search-text]").forEach(optionNode => {
        optionNode.hidden = query && !String(optionNode.dataset.searchText || "").includes(query);
      });
      return;
    }
    if (target.matches("[data-bind]")) {
      state.editor[target.dataset.bind] = target.value;
      state.editorDirty = true;
      return;
    }
    if (target.matches("[data-rule-field]")) {
      const rule = state.editor.rules[Number(target.dataset.index)];
      if (rule) setNestedValue(rule, target.dataset.ruleField, target.value);
      return;
    }
    if (target.matches("[data-condition-value]")) {
      updateConditionValue(target);
      return;
    }
    if (target.matches("[data-rule-copy]")) {
      const rule = state.editor.rules[Number(target.dataset.index)];
      if (rule) setNestedValue(rule.copy_parameters, target.dataset.ruleCopy, target.value);
      return;
    }
    if (target.matches("[data-schedule]")) { setNestedValue(state.editor.schedule, target.dataset.schedule, target.value); return; }
    if (target.matches("[data-quota]")) { setNestedValue(state.editor.quotas, target.dataset.quota, target.value); return; }
    if (target.matches("[data-selection]")) {
      setNestedValue(state.editor.selection, target.dataset.selection, target.value);
      if (target.dataset.selection === "metric_window_days") invalidateEstimate();
      return;
    }
    if (target.matches('[data-filter="keyword"]')) {
      state.filters.keyword = target.value;
      window.clearTimeout(filterTimer);
      filterTimer = window.setTimeout(() => { state.list.page = 1; loadRuleGroups(); }, 350);
      return;
    }
    if (target.matches("[data-log-filter]")) state.logFilters[target.dataset.logFilter] = target.value;
  }

  function handleChange(event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.matches("[data-filter]") && target.dataset.filter !== "keyword") {
      state.filters[target.dataset.filter] = target.value;
      state.list.page = 1;
      loadRuleGroups();
      return;
    }
    if (target.matches("[data-log-filter]")) {
      state.logFilters[target.dataset.logFilter] = target.value;
      if (target.tagName === "SELECT" || target.getAttribute("type") === "date") { state.logs.page = 1; loadExecutions(); }
      return;
    }
    if (target.matches("[data-bind]")) {
      state.editor[target.dataset.bind] = target.value;
      state.editorDirty = true;
      if (target.dataset.bind === "optimizer_id") invalidateEstimate();
      if (target.dataset.bind === "run_mode" || target.dataset.bind === "optimizer_id") renderEditor();
      return;
    }
    if (target.matches("[data-rule-field]")) {
      const rule = state.editor.rules[Number(target.dataset.index)];
      if (!rule) return;
      setNestedValue(rule, target.dataset.ruleField, target.value);
      if (target.dataset.ruleField === "action") {
        if (target.value !== "copy") rule.copy_parameters = {};
        renderEditor();
      }
      return;
    }
    if (target.matches("[data-condition-field]")) {
      const condition = conditionAt(target);
      if (!condition) return;
      if (target.dataset.conditionField === "field") { condition.field = target.value; condition.operator = ""; condition.value = ""; }
      else { condition.operator = target.value; condition.value = target.value === "between" ? ["", ""] : ""; }
      state.editorDirty = true; renderEditor(); return;
    }
    if (target.matches("[data-rule-copy]")) {
      const rule = state.editor.rules[Number(target.dataset.index)];
      if (!rule) return;
      if (target.dataset.ruleCopy === "budget_mode") {
        delete rule.copy_parameters.budget_multiplier;
        delete rule.copy_parameters.target_cpi;
        delete rule.copy_parameters.source_budget_ratio;
      }
      if (target.dataset.ruleCopy === "roas_adjustment_direction" && !target.value) delete rule.copy_parameters.roas_adjustment_percent;
      setNestedValue(rule.copy_parameters, target.dataset.ruleCopy, target.value);
      if (["budget_mode", "roas_adjustment_direction"].includes(target.dataset.ruleCopy)) renderEditor();
      return;
    }
    if (target.matches("[data-schedule]")) {
      setNestedValue(state.editor.schedule, target.dataset.schedule, target.value);
      if (target.dataset.schedule === "type") {
        delete state.editor.schedule.fixed_time; delete state.editor.schedule.interval_minutes;
        state.editor.schedule.type = target.value; renderEditor();
      }
      return;
    }
    if (target.matches("[data-quota]")) { setNestedValue(state.editor.quotas, target.dataset.quota, target.value); return; }
    if (target.matches("[data-selection]")) {
      setNestedValue(state.editor.selection, target.dataset.selection, target.value);
      if (target.dataset.selection === "metric_window_days") { invalidateEstimate(); renderEditor(); return; }
      if (target.dataset.selection === "mode") { if (target.value === "all" || !target.value) delete state.editor.selection.top_n; renderEditor(); }
      if (target.dataset.selection === "sort_field") { if (!target.value) delete state.editor.selection.sort_direction; renderEditor(); }
    }
  }

  function conditionAt(target) {
    const rule = state.editor && state.editor.rules[Number(target.dataset.ruleIndex)];
    return rule && rule.conditions[Number(target.dataset.conditionIndex)];
  }

  function updateConditionValue(target) {
    const condition = conditionAt(target);
    if (!condition) return;
    const slot = target.dataset.conditionValue;
    if (slot === "single") condition.value = target.value;
    else {
      if (!Array.isArray(condition.value)) condition.value = ["", ""];
      condition.value[Number(slot)] = target.value;
    }
    state.editorDirty = true;
  }

  async function handleClick(event) {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const guardedLink = target.closest("a[data-guard-editor-exit]");
    if (guardedLink) {
      event.preventDefault();
      await requestGuardedNavigation(guardedLink.href);
      return;
    }
    const actionNode = target.closest("[data-action]");
    if (state.openMulti && !target.closest("[data-multi-root]") && (!actionNode || !["multi-option", "toggle-multi"].includes(actionNode.dataset.action))) {
      state.openMulti = "";
      if (!actionNode) { renderCurrentPage(); return; }
    }
    if (state.openSingle && !target.closest("[data-single-root]") && (!actionNode || !["single-option", "toggle-single"].includes(actionNode.dataset.action))) {
      state.openSingle = "";
      if (!actionNode) { renderCurrentPage(); return; }
    }
    if (!actionNode) return;
    const action = actionNode.dataset.action;
    if (action === "close-detail" && actionNode.classList.contains("detail-overlay") && target !== actionNode) return;
    if (action === "retry-init") { window.location.reload(); return; }
    if (action === "new-group") {
      invalidateEstimate(); state.editor = newEditor(); state.editorStep = 1; state.editorDirty = false; renderEditor(); return;
    }
    if (action === "close-editor") { await requestCloseEditor(); return; }
    if (action === "goto-step") { state.editorStep = asNumber(actionNode.dataset.step, 1); renderEditor(); return; }
    if (action === "previous-step") { state.editorStep = Math.max(1, state.editorStep - 1); renderEditor(); return; }
    if (action === "next-step") { state.editorStep = Math.min(5, state.editorStep + 1); renderEditor(); return; }
    if (action === "select-channel") { state.editor.channel = actionNode.dataset.value; state.editorDirty = true; invalidateEstimate(); renderEditor(); return; }
    if (action === "select-level") { selectObjectLevel(actionNode.dataset.value); return; }
    if (action === "toggle-multi") { state.openSingle = ""; state.openMulti = state.openMulti === actionNode.dataset.name ? "" : actionNode.dataset.name; renderEditor(); return; }
    if (action === "multi-option") { toggleMultiOption(actionNode.dataset.name, actionNode.dataset.value); return; }
    if (action === "toggle-single") {
      const singleName = actionNode.dataset.name;
      state.openMulti = "";
      state.openSingle = state.openSingle === singleName ? "" : singleName;
      renderCurrentPage();
      if (state.openSingle) window.requestAnimationFrame(() => document.querySelector(`[data-single-search="${singleName}"]`)?.focus());
      return;
    }
    if (action === "single-option") { await selectSingleOption(actionNode.dataset.name, actionNode.dataset.value); return; }
    if (action === "estimate-scope") { await estimateScope(); return; }
    if (action === "add-rule") { addRule(); return; }
    if (action === "remove-rule") { state.editor.rules.splice(Number(actionNode.dataset.index), 1); state.editorDirty = true; renderEditor(); return; }
    if (action === "add-condition") { const rule = state.editor.rules[Number(actionNode.dataset.index)]; if (rule) { rule.conditions.push({ field: "", operator: "", value: "" }); state.editorDirty = true; renderEditor(); } return; }
    if (action === "remove-condition") { const rule = state.editor.rules[Number(actionNode.dataset.ruleIndex)]; if (rule) { rule.conditions.splice(Number(actionNode.dataset.conditionIndex), 1); state.editorDirty = true; renderEditor(); } return; }
    if (action === "save-draft") { await saveEditor(); return; }
    if (action === "save-preview") { await saveEditor({ preview: true }); return; }
    if (action === "reload-groups") { await loadRuleGroups(); return; }
    if (action === "reset-rule-filters") { state.filters = {}; state.list.page = 1; await loadRuleGroups(); return; }
    if (action === "rule-page-prev") { state.list.page = Math.max(1, state.list.page - 1); await loadRuleGroups(); return; }
    if (action === "rule-page-next") { state.list.page += 1; await loadRuleGroups(); return; }
    if (action === "edit-group") { await editGroup(actionNode.dataset.id); return; }
    if (action === "preview-group") { await previewGroup(actionNode.dataset.id, false); return; }
    if (action === "execute-group") { await executeGroup(actionNode.dataset.id); return; }
    if (action === "duplicate-group") { await duplicateGroup(actionNode.dataset.id); return; }
    if (action === "toggle-group") { await toggleGroup(actionNode); return; }
    if (action === "emergency-group") { await emergencyGroup(actionNode.dataset.id); return; }
    if (action === "delete-group") { await deleteGroup(actionNode.dataset.id); return; }
    if (action === "reload-logs" || action === "apply-log-filters") { state.logs.page = 1; await loadExecutions(); return; }
    if (action === "reset-log-filters") { state.logFilters = {}; state.logs.page = 1; await loadExecutions(); return; }
    if (action === "log-page-prev") { state.logs.page = Math.max(1, state.logs.page - 1); await loadExecutions(); return; }
    if (action === "log-page-next") { state.logs.page += 1; await loadExecutions(); return; }
    if (action === "open-log-detail") { await openExecutionDetail(actionNode.dataset.id); return; }
    if (action === "close-detail") { closeDetail(); return; }
    if (action === "dialog-cancel") { closeDialog(false); return; }
    if (action === "dialog-confirm") { closeDialog(true); }
  }

  async function allowShellNavigation() {
    if (isInFlight("editor-save")) { toast("规则正在保存或试算，请等待完成后再离开。", "error"); return; }
    if (state.editorDirty) {
      const confirmed = await confirmDialog({ title: "离开并放弃未保存的更改？", message: "当前规则配置尚未保存。离开本页后，本次更改不会保留。", confirmLabel: "放弃更改并离开", danger: true });
      if (!confirmed) return false;
    }
    state.editorDirty = false;
    return true;
  }

  async function requestGuardedNavigation(url) {
    if (!(await allowShellNavigation())) return;
    window.location.assign(url);
  }

  function selectObjectLevel(value) {
    if (!LEVEL_LABELS[value]) return;
    if (state.editor.object_level !== value) {
      state.editor.object_level = value;
      const allowed = new Set(state.meta.fields.filter(field => (!field.levels.length || field.levels.includes(value)) && field.filterable !== false && field.previewable !== false).map(field => field.key));
      state.editor.rules.forEach(rule => {
        rule.conditions = rule.conditions.filter(condition => allowed.has(condition.field));
        if (rule.copy_parameters) delete rule.copy_parameters.carrier_strategy;
      });
      invalidateEstimate();
      state.editorDirty = true;
    }
    renderEditor();
  }

  function toggleMultiOption(name, value) {
    const list = array(state.editor[name]);
    const index = list.indexOf(value);
    if (index >= 0) list.splice(index, 1); else list.push(value);
    state.editor[name] = list;
    state.editorDirty = true;
    if (["products", "account_timezones"].includes(name)) invalidateEstimate();
    renderEditor();
  }

  async function selectSingleOption(name, value) {
    const selected = String(value || "");
    state.openSingle = "";
    if (name === "editor-optimizer") {
      state.editor.optimizer_id = selected;
      state.editorDirty = true;
      invalidateEstimate();
      renderEditor();
      return;
    }
    if (name === "rule-product" || name === "rule-optimizer") {
      state.filters[name === "rule-product" ? "product" : "optimizer_id"] = selected;
      state.list.page = 1;
      renderRuleGroupShell();
      await loadRuleGroups();
      return;
    }
    if (name === "log-product" || name === "log-optimizer") {
      state.logFilters[name === "log-product" ? "product" : "optimizer_id"] = selected;
      state.logs.page = 1;
      renderLogShell();
      await loadExecutions();
    }
  }

  function addRule() {
    if (!state.editor.object_level) { toast("请先选择调控对象层级。", "error"); return; }
    state.editor.rules.push(newRuleDraft(state.editor.rules.length));
    state.editorDirty = true; renderEditor();
  }

  async function requestCloseEditor() {
    if (state.editorDirty) {
      const confirmed = await confirmDialog({ title: "放弃未保存的更改？", message: "返回列表后，本次未保存的规则配置不会保留。", confirmLabel: "放弃更改", danger: true });
      if (!confirmed) return;
    }
    invalidateEstimate(); state.editor = null; state.editorDirty = false; state.openMulti = ""; state.openSingle = ""; renderRuleGroupShell(); await loadRuleGroups();
  }

  async function editGroup(id) {
    if (!id) return;
    const group = listedGroup(id);
    if (group && !canMutateGroup(group, state.actor)) { toast("该规则组不属于当前用户，只能查看。", "error"); return; }
    try {
      const payload = await api(`/rule-groups/${encodeURIComponent(id)}`);
      invalidateEstimate(); state.editor = normalizeGroupForEditor(payload); state.editorStep = 1; state.editorDirty = false; renderEditor();
    } catch (error) { toast(errorMessage(error), "error"); }
  }

  async function duplicateGroup(id) {
    const flightKey = beginGroupWrite(id);
    if (!flightKey) return;
    try {
      const confirmed = await confirmDialog({ title: "复制这个规则组？", message: "副本会使用相同配置，但始终保持停用并进入只观察模式。", confirmLabel: "创建副本" });
      if (!confirmed) return;
      await api(`/rule-groups/${encodeURIComponent(id)}/duplicate`, { method: "POST", body: "{}" }); toast("规则组副本已创建。", "success"); await loadRuleGroups();
    } catch (error) { toast(errorMessage(error), "error"); }
    finally { finishGroupWrite(flightKey); }
  }

  async function toggleGroup(node) {
    const currentlyEnabled = node.dataset.enabled === "true";
    const enabling = !currentlyEnabled;
    if (enabling && !(state.meta && state.meta.permissions && state.meta.permissions.canEnable)) {
      toast("计划调度器尚未发布，当前仅支持保存草稿和手动试算，不能持续自动扫描。", "error");
      return;
    }
    const flightKey = beginGroupWrite(node.dataset.id);
    if (!flightKey) return;
    const live = node.dataset.mode === "live";
    try {
      const confirmed = await confirmDialog({
        title: enabling ? (live ? "确认启用正式执行？" : "确认启用观察规则？") : "确认停用规则？",
        message: enabling ? (live ? "正式执行可能修改 Meta 对象。必须先有有效试算，并输入确认短语。" : "启用后会按计划持续扫描并记录观察结果，不会调用 Meta 写接口。") : "停用后，runner 将不再调度这个规则组。",
        confirmLabel: enabling ? "确认启用" : "确认停用", danger: enabling && live,
        phrase: enabling && live ? "ENABLE_LIVE_MODE" : "",
      });
      if (!confirmed) return;
      await api(`/rule-groups/${encodeURIComponent(node.dataset.id)}/enabled`, { method: "POST", body: JSON.stringify({ enabled: enabling, confirm: typeof confirmed === "string" ? confirmed : "" }) });
      toast(enabling ? "规则组已启用。" : "规则组已停用。", "success"); await loadRuleGroups();
    } catch (error) { toast(errorMessage(error), "error"); }
    finally { finishGroupWrite(flightKey); }
  }

  async function emergencyGroup(id) {
    const flightKey = beginGroupWrite(id);
    if (!flightKey) return;
    try {
      const confirmed = await confirmDialog({ title: "紧急停止这个规则组？", message: "急停会立即阻止后续调度。恢复时必须重新试算并显式启用。", confirmLabel: "紧急停止", danger: true });
      if (!confirmed) return;
      await api(`/rule-groups/${encodeURIComponent(id)}/emergency-stop`, { method: "POST", body: "{}" }); toast("规则组已紧急停止。", "success"); await loadRuleGroups();
    } catch (error) { toast(errorMessage(error), "error"); }
    finally { finishGroupWrite(flightKey); }
  }

  async function deleteGroup(id) {
    const flightKey = beginGroupWrite(id);
    if (!flightKey) return;
    try {
      const confirmed = await confirmDialog({ title: "删除这个规则组？", message: "规则组会被软删除，历史执行日志与审计信息仍会保留。", confirmLabel: "删除规则组", danger: true });
      if (!confirmed) return;
      await api(`/rule-groups/${encodeURIComponent(id)}`, { method: "DELETE", body: "{}" }); toast("规则组已删除。", "success"); await loadRuleGroups();
    } catch (error) { toast(errorMessage(error), "error"); }
    finally { finishGroupWrite(flightKey); }
  }

  function beginGroupWrite(id) {
    const group = listedGroup(id);
    if (group && !canMutateGroup(group, state.actor)) { toast("该规则组不属于当前用户，只能查看。", "error"); return ""; }
    const key = `group-write:${id}`;
    if (!beginInFlight(key)) return "";
    renderRuleGroupShell();
    return key;
  }

  function finishGroupWrite(key) {
    endInFlight(key);
    if (!state.editor) renderRuleGroupShell();
  }

  function confirmDialog(options) {
    closeDialog(false);
    const settings = Object.assign({ title: "请确认", message: "", confirmLabel: "确认", danger: false, phrase: "" }, options || {});
    const root = document.getElementById("dialogRoot");
    if (!root) return Promise.resolve(false);
    root.innerHTML = `<div class="dialog-backdrop"><section class="dialog" role="dialog" aria-modal="true" aria-labelledby="confirmDialogTitle"><div class="dialog-head"><h2 id="confirmDialogTitle">${h(settings.title)}</h2></div><div class="dialog-body"><p>${h(settings.message)}</p>${settings.phrase ? `<div class="field"><label for="confirmPhrase">输入确认短语</label><input id="confirmPhrase" type="text" autocomplete="off" placeholder="输入 ${h(settings.phrase)}" data-confirm-phrase="${h(settings.phrase)}"><p class="field-hint">必须完全一致：${h(settings.phrase)}</p></div>` : ""}</div><div class="dialog-actions"><button class="button" type="button" data-action="dialog-cancel">取消</button><button class="button ${settings.danger ? "button-danger" : "button-primary"}" type="button" data-action="dialog-confirm"${settings.phrase ? " disabled" : ""}>${h(settings.confirmLabel)}</button></div></section></div>`;
    if (settings.phrase) {
      const input = root.querySelector("[data-confirm-phrase]");
      const button = root.querySelector('[data-action="dialog-confirm"]');
      input?.addEventListener("input", () => { button.disabled = input.value !== settings.phrase; });
    }
    window.requestAnimationFrame(() => (root.querySelector("input") || root.querySelector('[data-action="dialog-cancel"]'))?.focus());
    return new Promise(resolve => { dialogResolver = { resolve, phrase: settings.phrase, root }; });
  }

  function closeDialog(confirmed) {
    if (!dialogResolver) {
      const root = document.getElementById("dialogRoot"); if (root) root.innerHTML = ""; return;
    }
    const current = dialogResolver; dialogResolver = null;
    let result = Boolean(confirmed);
    if (confirmed && current.phrase) result = current.root.querySelector("[data-confirm-phrase]")?.value || false;
    current.root.innerHTML = "";
    current.resolve(result);
  }

  window.AdControlV3Ui = Object.freeze({
    escapeHtml: h,
    normalizeMeta,
    capabilityBannerCopy,
    normalizeGroup: normalizeGroupForEditor,
    executionIdOf,
    newRuleDraft,
    canToggleGroup,
    canMutateGroup,
    beginInFlight,
    endInFlight,
    isInFlight,
    scopeFingerprint,
    requestGuardedNavigation,
    executionValue,
    displayCount,
    conditionValueSpec,
    selectionValidationErrors,
    firstInvalidStep,
    saveRequestForEditor,
    previewPath,
    editorPayload: () => state.editor ? editorPayload() : null,
  });
})();
