(function () {
  const PAGE = document.body.dataset.page || "overview";
  const TITLES = {
    overview: ["AI自动规则调控", "查看调控中心状态、风险提示和常用入口。", "adControl"],
    rules: ["规则组管理", "按账号配置调控对象、命中动作、运行模式和自动复制策略。", "adControlRules"],
    pools: ["账户池", "按产品创建账户池，后续在绑定关系中复用。", "adControlPools"],
    bindings: ["绑定关系", "配置产品、账户池和规则集的绑定，并控制启停。", "adControlBindings"],
    run: ["运行控制台已下线", "规则组现在直接在管理页启停，不再使用 Preview 流程。", "adControlRules"],
    tokens: ["默认Token来源", "按目标产品读取 apps_setting.default_user。", "adControlTokens"],
    logs: ["执行日志", "查看调控流程、Meta 执行结果、续跑状态和日志存储状态。", "adControlLogs"],
  };
  const defaultRules = [
    { name: "7小时+无安装关闭", action: "pause", enabled: true, window: { type: "since_start" }, conditions: [
      { field: "age_hours", op: "gte", value: 7 },
      { field: "install", op: "lte", value: 0 },
    ] },
    { name: "消耗30+安装10-ROAS30-关闭", action: "pause", enabled: true, window: { type: "since_start" }, conditions: [
      { field: "spend", op: "gt", value: 30 },
      { field: "install", op: "lte", value: 10 },
      { field: "roas_pct", op: "lt", value: 30 },
    ] },
  ];
  const COUNTRY_GROUP_STORAGE_KEY = "adControlCountryGroups";
  const ALLOWED_PRODUCTS = ["dramawave", "hotdrama", "freereels"];
  const PRODUCT_LABELS = {
    dramawave: "dramawave",
    hotdrama: "hotdrama",
    freereels: "freereels",
  };
  const OBJECT_LEVEL_LABELS = {
    campaign: "广告系列 Campaign",
    ad: "广告 Ad",
  };
  const RUN_MODE_LABELS = {
    observe: "观察模式",
    live: "正式执行",
  };
  const defaultCountryGroups = [];
  const defaultTimezones = ["8", "+8", "UTC+8"];
  const defaultExcludedLanguages = ["JA", "KO", "ZHTW", "TH", "ID", "VI", "MS", "TL"];
  const state = {
    auth: null,
    products: [],
    accounts: [],
    pools: [],
    ruleSets: [],
    bindings: [],
    ruleGroupAccounts: [],
    frontendRuleGroups: [],
    ruleGroupDraft: null,
    logLoadSequence: 0,
  };
  const $ = id => document.getElementById(id);

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function toast(message, variant) {
    const node = $("toast");
    node.textContent = message;
    node.classList.toggle("error", variant === "error");
    node.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.remove("show"), variant === "error" ? 5200 : 2600);
  }
  async function api(url, options) {
    const response = await fetch(url, Object.assign({ credentials: "same-origin" }, options || {}));
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || data.error || "请求失败");
      error.code = data.code || "request_failed";
      error.details = data;
      throw error;
    }
    return data;
  }
  function hasPermission(auth) {
    const user = auth && auth.user;
    return !!(user && (user.is_admin || (user.permissions && user.permissions.ad_control_center)));
  }
  function product() {
    return ($("productSelect") || {}).value || "";
  }
  function productOptions() {
    return ALLOWED_PRODUCTS.map(value => ({ product: value, label: PRODUCT_LABELS[value] || value }));
  }
  function selectedAccounts() {
    return Array.from(new Set(Array.from(document.querySelectorAll("#accountList input[type=checkbox]:checked")).map(item => normalizeAccountId(item.value)).filter(Boolean)));
  }
  function money(value) {
    return Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  function splitValues(value) {
    return String(value || "").split(/[,，\s]+/).map(item => item.trim()).filter(Boolean);
  }
  function countryGroups() {
    try {
      const values = JSON.parse(localStorage.getItem(COUNTRY_GROUP_STORAGE_KEY) || "[]");
      if (Array.isArray(values) && values.length) return values;
    } catch (error) {}
    return defaultCountryGroups.slice();
  }
  function saveCountryGroups(values) {
    const clean = Array.from(new Set((values || []).map(item => String(item || "").trim()).filter(Boolean)));
    localStorage.setItem(COUNTRY_GROUP_STORAGE_KEY, JSON.stringify(clean));
    return clean;
  }
  function countryGroupLabel(values) {
    return Array.isArray(values) && values.length ? values.join(", ") : "不限国家组";
  }
  function timezoneValues(value) {
    const text = String(value || "").trim();
    if (!text) return new Set();
    const values = new Set([text, text.toUpperCase()]);
    const match = text.match(/([+-]?\d{1,2})(?:[:.]?(\d{1,2}))?$/);
    if (match) {
      const hour = Number(match[1]);
      const minute = Number((match[2] || "0").slice(0, 2));
      if (Number.isFinite(hour) && minute === 0) {
        values.add(String(hour));
        values.add(`${hour >= 0 ? "+" : ""}${hour}`);
        values.add(`UTC${hour >= 0 ? "+" : ""}${hour}`);
        values.add(`GMT${hour >= 0 ? "+" : ""}${hour}`);
      }
    }
    return new Set(Array.from(values).map(item => item.toUpperCase()));
  }
  function isPlus8Timezone(value) {
    const values = timezoneValues(value);
    return ["8", "+8", "UTC+8", "GMT+8"].some(item => values.has(item));
  }
  function selectedProducts(containerId) {
    return Array.from(document.querySelectorAll(`#${containerId} input[type=checkbox]:checked`)).map(input => input.value);
  }
  function productLabel(item) {
    item = item || {};
    const explicit = String(item.label || "").trim();
    if (explicit) return explicit;
    const parts = [];
    [item.name, item.product_value || item.product, item.app_id || item.app_package].forEach(value => {
      value = String(value || "").trim();
      if (value && !parts.includes(value)) parts.push(value);
    });
    return parts.length ? parts.join(" / ") : String(item.product || "");
  }
  function renderProductChecks(containerId, products, selected) {
    const node = $(containerId);
    if (!node) return;
    const selectedSet = new Set(selected || []);
    node.innerHTML = (products || []).length ? products.map(item => {
      const value = item.product || "";
      const checked = selectedSet.has(value) ? "checked" : "";
      return `<label class="check-option"><input type="checkbox" value="${escapeHtml(value)}" ${checked} /><span>${escapeHtml(productLabel(item))}</span></label>`;
    }).join("") : `<div class="empty">暂无产品</div>`;
  }
  function strategySummary(strategy) {
    strategy = strategy || {};
    const parts = [];
    const schedule = strategy.schedule || {};
    const limits = strategy.limits || {};
    const candidateSelection = strategy.candidate_selection || strategy.copy?.candidate_selection || {};
    if (schedule.type === "interval") parts.push(`每隔 ${schedule.interval_minutes || "--"} 分钟`);
    else if (schedule.time || strategy.close_time) parts.push(`每天 ${schedule.time || strategy.close_time}`);
    if (schedule.allowed_start || schedule.allowed_end || schedule.execute_before) parts.push(`${schedule.allowed_start || "00:00"}-${schedule.allowed_end || schedule.execute_before || "23:59"}`);
    if (limits.rule_daily_limit) parts.push(`规则日限 ${limits.rule_daily_limit}`);
    if (candidateSelection.mode === "all") parts.push("全部符合条件");
    else if (candidateSelection.mode === "top_n_per_account" || strategy.top_n_per_account) parts.push(`每账号 Top ${candidateSelection.top_n || strategy.top_n_per_account || 1}`);
    if (strategy.close_time) parts.push(`关闭 ${strategy.close_time}`);
    if (strategy.execute_timezone) parts.push(`时区 ${strategy.execute_timezone}`);
    if (strategy.block_same_day_reopen) parts.push("当天禁止重启");
    if (strategy.allow_next_day_reopen) parts.push("隔天允许重启");
    if (Array.isArray(strategy.country_groups) && strategy.country_groups.length) parts.push(`国家组 ${strategy.country_groups.join(",")}`);
    return parts.join(" / ") || "--";
  }
  function safeIdPart(value) {
    return String(value || "").trim().replace(/[^a-zA-Z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80) || "item";
  }
  function newFrontendRuleGroupId() {
    return `frg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  }
  function bindingId(item) {
    return (item && (item.binding_id || item.group_id)) || "";
  }
  function productBadgeList(products) {
    const values = Array.from(new Set((products || []).filter(Boolean)));
    return values.length ? values.map(value => `<span class="badge">${escapeHtml(PRODUCT_LABELS[value] || value)}</span>`).join("") : `<span class="hint">未设置</span>`;
  }
  function bindingFrontendGroupId(binding) {
    const strategy = (binding && binding.strategy) || {};
    return strategy.frontend_rule_group_id || bindingId(binding);
  }
  function bindingFrontendGroupName(binding) {
    const strategy = (binding && binding.strategy) || {};
    return strategy.frontend_rule_group_name || binding.name || bindingId(binding);
  }
  function aggregateRuleGroups(bindings) {
    const map = new Map();
    (bindings || []).forEach(binding => {
      const id = bindingFrontendGroupId(binding);
      const strategy = binding.strategy || {};
      if (!map.has(id)) {
        map.set(id, {
          id,
          name: bindingFrontendGroupName(binding),
          description: strategy.description || "",
          bindings: [],
          target_ids: [],
          account_ids: [],
          country_groups: Array.isArray(strategy.country_groups) ? strategy.country_groups : [],
          close_time: strategy.close_time || "",
          execute_timezone: strategy.execute_timezone || "",
          block_same_day_reopen: !!strategy.block_same_day_reopen,
          allow_next_day_reopen: !!strategy.allow_next_day_reopen,
          enabled: false,
          enabled_count: 0,
          partial_enabled: false,
          reported_partial_enabled: false,
          emergency_stopped: false,
          rule_count: 0,
          account_count: 0,
          object_level: binding.object_level || strategy.object_level || strategy.target_level || "campaign",
          run_mode: binding.run_mode || strategy.run_mode || strategy.execution_mode || "observe",
          has_copy_action: false,
          updated_at: binding.updated_at || "",
        });
      }
      const group = map.get(id);
      group.bindings.push(binding);
      const bindingTargetIds = (Array.isArray(binding.target_ids) ? binding.target_ids : [bindingId(binding)])
        .map(value => String(value || "").trim()).filter(Boolean);
      bindingTargetIds.forEach(targetId => {
        if (!group.target_ids.includes(targetId)) group.target_ids.push(targetId);
      });
      if (!group.description && strategy.description) group.description = strategy.description;
      if (!group.close_time && strategy.close_time) group.close_time = strategy.close_time;
      if (!group.execute_timezone && strategy.execute_timezone) group.execute_timezone = strategy.execute_timezone;
      group.block_same_day_reopen = group.block_same_day_reopen || !!strategy.block_same_day_reopen;
      group.allow_next_day_reopen = group.allow_next_day_reopen || !!strategy.allow_next_day_reopen;
      if (binding.enabled_count != null && Number.isFinite(Number(binding.enabled_count))) {
        group.enabled_count += Number(binding.enabled_count);
      } else if (binding.enabled) {
        group.enabled_count += Math.max(1, bindingTargetIds.length);
      }
      group.reported_partial_enabled = group.reported_partial_enabled || binding.partial_enabled === true;
      group.emergency_stopped = group.emergency_stopped || !!binding.emergency_stopped;
      const rules = Array.isArray(binding.rules) ? binding.rules : [];
      group.rule_count = Math.max(group.rule_count, rules.length);
      group.has_copy_action = group.has_copy_action || rules.some(rule => String(rule.action || "").toLowerCase() === "copy");
      const accountIds = Array.isArray(strategy.selected_account_ids) ? strategy.selected_account_ids : (Array.isArray(binding.account_ids) ? binding.account_ids : []);
      accountIds.map(normalizeAccountId).filter(Boolean).forEach(accountId => {
        if (!group.account_ids.includes(accountId)) group.account_ids.push(accountId);
      });
      group.account_count = group.account_ids.length;
      group.object_level = binding.object_level || strategy.object_level || strategy.target_level || group.object_level;
      group.run_mode = binding.run_mode || strategy.run_mode || strategy.execution_mode || group.run_mode;
      if (binding.updated_at && (!group.updated_at || binding.updated_at > group.updated_at)) group.updated_at = binding.updated_at;
      if ((!group.country_groups || !group.country_groups.length) && Array.isArray(strategy.country_groups)) group.country_groups = strategy.country_groups;
    });
    return Array.from(map.values()).map(group => {
      const total = group.target_ids.length || group.bindings.length;
      group.partial_enabled = group.reported_partial_enabled || (group.enabled_count > 0 && group.enabled_count < total);
      group.enabled = !group.partial_enabled && total > 0 && group.enabled_count >= total;
      return group;
    }).sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
  }
  function defaultCrossRegionRules() {
    return [{
      name: "+8跨区国家组关停",
      action: "pause",
      enabled: true,
      window: { type: "since_start" },
      conditions: [
        { field: "account_time_zone", op: "in", value: defaultTimezones.slice() },
        { field: "language", op: "not_in", value: defaultExcludedLanguages.slice() },
      ],
    }];
  }
  function primaryRuleFromDraft(draft) {
    return Array.isArray((draft || {}).rules) && draft.rules.length && typeof draft.rules[0] === "object" ? draft.rules[0] : {};
  }
  function firstCopyRuleIndex(rules) {
    return (Array.isArray(rules) ? rules : []).findIndex(rule => String(rule?.action || "").trim().toLowerCase() === "copy");
  }
  function mergeGeneratedPrimaryRule(existingRules, generatedRule) {
    const rules = Array.isArray(existingRules) ? existingRules.slice() : [];
    const previous = rules.length && rules[0] && typeof rules[0] === "object" ? rules[0] : {};
    const next = Object.assign({}, previous, generatedRule || {});
    if (String(next.action || "").toLowerCase() !== "copy") {
      delete next.copy;
      delete next.copy_config;
      delete next.drama_scope;
      delete next.candidate_selection;
      delete next.top_n_per_account;
    }
    if (rules.length) rules[0] = next;
    else rules.push(next);
    return rules;
  }
  function ruleConditions(rule) {
    return Array.isArray((rule || {}).conditions) ? rule.conditions : [];
  }
  function conditionFor(rule, fields, ops) {
    const fieldSet = new Set((Array.isArray(fields) ? fields : [fields]).map(item => String(item || "").toLowerCase()));
    const opSet = new Set((Array.isArray(ops) ? ops : [ops]).map(item => String(item || "").toLowerCase()));
    return ruleConditions(rule).find(condition => fieldSet.has(String(condition.field || "").toLowerCase()) && (!opSet.size || opSet.has(String(condition.op || condition.operator || "").toLowerCase())));
  }
  function conditionList(rule, fields, ops, fallback) {
    const condition = conditionFor(rule, fields, ops);
    if (!condition) return (fallback || []).slice();
    return Array.isArray(condition.value) ? condition.value : splitValues(condition.value);
  }
  function conditionNumber(rule, fields, ops, fallback) {
    const condition = conditionFor(rule, fields, ops);
    if (!condition || condition.value == null || condition.value === "") return fallback == null ? "" : fallback;
    return condition.value;
  }
  function ruleGroupBindingId(frontendId, productValue) {
    return `${safeIdPart(frontendId)}_${safeIdPart(productValue)}_binding`;
  }
  function ruleGroupRuleSetId(frontendId, productValue) {
    return `${safeIdPart(frontendId)}_${safeIdPart(productValue)}_rules`;
  }
  function ruleGroupAccountGroupId(frontendId, productValue) {
    return `${safeIdPart(frontendId)}_${safeIdPart(productValue)}_accounts`;
  }
  function requireSharedUi() {
    if (!window.UiTopbar) throw new Error("公共顶吸脚本 /ui-topbar.js 未加载");
    if (!window.QuickNav) throw new Error("公共快速导航脚本 /quick-nav.js 未加载");
  }
  function quickNavOptions(auth) {
    return {
      container: $("quickNav"),
      auth,
      activeKey: TITLES[PAGE][2],
      onNavigate: item => {
        if (item && item.href) window.location.assign(item.href);
      },
    };
  }
  function renderWarmQuickNav() {
    if (!window.QuickNav || !$("quickNav")) return;
    window.QuickNav.render(quickNavOptions(null)).catch(error => console.warn("预渲染快速导航失败", error));
  }
  async function loadAuth() {
    requireSharedUi();
    state.auth = await api("/api/ui/topbar");
    window.UiTopbar.render({
      auth: state.auth,
      userCard: $("userCard"),
      authButton: $("authBtn"),
      refreshButton: $("refreshBtn"),
      loginText: "登录",
      logoutText: "退出登录",
    });
    await window.QuickNav.render(quickNavOptions(state.auth));
    $("loginGate").classList.toggle("hidden", !!state.auth.authenticated);
    $("permissionGate").classList.toggle("hidden", !state.auth.authenticated || hasPermission(state.auth));
    $("pageRoot").classList.toggle("hidden", !state.auth.authenticated || !hasPermission(state.auth));
    return state.auth.authenticated && hasPermission(state.auth);
  }
  async function loadProducts(options = {}) {
    const includeAll = !!options.includeAll;
    state.products = productOptions();
    const select = $("productSelect");
    if (select) {
      const previous = select.value;
      const items = includeAll
        ? [{ product: "", label: "全部产品（含账号规则）" }].concat(state.products)
        : state.products;
      select.innerHTML = items.map(item => `<option value="${escapeHtml(item.product)}">${escapeHtml(productLabel(item))}</option>`).join("");
      if (includeAll && !previous) select.value = "";
      else if (previous && ALLOWED_PRODUCTS.includes(previous)) select.value = previous;
    }
  }
  async function loadAccounts(selected) {
    if (!product()) return;
    const preserveSelected = Array.isArray(selected) ? selected : selectedAccounts();
    const data = await api(`/api/ad-control/accounts?product=${encodeURIComponent(product())}`);
    state.accounts = data.items || [];
    renderAccounts(preserveSelected);
    return data;
  }
  function renderAccounts(selected) {
    const list = $("accountList");
    if (!list) return;
    const selectedIds = Array.from(new Set((selected || []).map(normalizeAccountId).filter(Boolean)));
    const selectedSet = new Set(selectedIds);
    const knownIds = new Set((state.accounts || []).map(item => normalizeAccountId(item.account_id || item.ad_account_id || "")).filter(Boolean));
    const manualIds = selectedIds.filter(id => !knownIds.has(id));
    const manualHtml = manualIds.map(id => `<label class="account-option manual-account-option"><input type="checkbox" value="${escapeHtml(id)}" checked /><div><div class="account-title">手动账号 ${escapeHtml(id)}</div><div class="account-meta">${escapeHtml(id)} / 手动添加</div></div><button class="btn" type="button" data-remove-pool-account="${escapeHtml(id)}">移除</button></label>`).join("");
    const accountHtml = state.accounts.length ? state.accounts.map(item => {
      const id = item.account_id || item.ad_account_id || "";
      const normalizedId = normalizeAccountId(id);
      const checked = selectedSet.has(normalizedId) ? "checked" : "";
      return `<label class="account-option"><input type="checkbox" value="${escapeHtml(id)}" ${checked} /><div class="account-title">${escapeHtml(item.account_name || item.name || id)}</div><div class="account-meta">${escapeHtml(id)} / ${escapeHtml(item.time_zone || "--")}</div></label>`;
    }).join("") : `<div class="empty">暂无接口账户，可手动添加账号 ID</div>`;
    list.innerHTML = manualHtml || accountHtml ? `${manualHtml}${accountHtml}` : `<div class="empty">暂无账户</div>`;
  }
  async function loadPools() {
    const data = await api(`/api/ad-control/account-groups?product=${encodeURIComponent(product())}`);
    state.pools = data.items || [];
  }
  async function loadRuleSets() {
    const data = await api(`/api/ad-control/rule-sets?product=${encodeURIComponent(product())}`);
    state.ruleSets = data.items || [];
  }
  async function loadBindings() {
    const data = await api(`/api/ad-control/bindings?product=${encodeURIComponent(product())}`);
    state.bindings = data.items || [];
  }
  function optionHtml(items, valueKey, labelKey, emptyLabel) {
    const body = (items || []).map(item => `<option value="${escapeHtml(item[valueKey] || "")}">${escapeHtml(item[labelKey] || item[valueKey] || "")}</option>`).join("");
    return `<option value="">${escapeHtml(emptyLabel || "请选择")}</option>${body}`;
  }
  function pageHeaderActions() {
    return `<div class="top-actions"><div id="userCard" class="user-card"></div><button class="btn btn-secondary" id="refreshBtn" type="button">刷新</button><button class="btn btn-primary" id="authBtn" type="button">退出登录</button></div>`;
  }
  function productFilter(extra = "") {
    return `<section class="panel"><div class="panel-body"><div class="grid ${extra ? "two" : ""}"><div class="field"><label for="productSelect">产品</label><select id="productSelect"></select></div>${extra}</div></div></section>`;
  }

  async function renderOverview() {
    $("pageRoot").innerHTML = `
      <section class="panel"><div class="panel-head"><h2>状态概览</h2><span class="hint" id="runnerStatus">加载中...</span></div><div class="panel-body">
        <div class="cards"><div class="metric"><span>启用绑定</span><strong id="enabledBindings">0</strong></div><div class="metric"><span>急停绑定</span><strong id="stoppedBindings">0</strong></div><div class="metric"><span>资源阈值</span><strong id="resourceLimit">--</strong></div><div class="metric"><span>最大并发</span><strong id="maxWorkers">--</strong></div></div>
        <div class="risk">默认 token 来源读取目标产品 apps_setting.default_user；规则执行前请先确认账户池、规则组和执行日志。</div>
      </div></section>
      <section class="panel"><div class="panel-head"><h2>快速入口</h2></div><div class="panel-body"><div class="cards">
        ${quickCard("规则组管理", "/ad-control-rules.html", "维护可复用规则")}
        ${quickCard("账户池", "/ad-control-account-pools.html", "选择产品账户")}
        ${quickCard("执行日志", "/ad-control-logs.html", "查看审计结果")}
      </div></div></section>
      <section class="panel"><div class="panel-head"><h2>最近执行</h2><a class="btn" href="/ad-control-logs.html">查看全部</a></div><div class="panel-body"><div class="list" id="latestActions"></div></div></section>`;
    const runner = await api("/api/ad-control/runner/status").catch(error => ({ error: error.message }));
    if (!runner.error) {
      $("runnerStatus").textContent = `CPU ${money((runner.resource || {}).cpu_percent)}% / 内存 ${money((runner.resource || {}).mem_percent)}%`;
      $("enabledBindings").textContent = runner.enabled_rule_groups || 0;
      $("stoppedBindings").textContent = runner.emergency_stopped_groups || 0;
      $("resourceLimit").textContent = `${runner.resource_limit_percent || "--"}%`;
      $("maxWorkers").textContent = runner.max_workers || "--";
    } else {
      $("runnerStatus").textContent = runner.error;
    }
    const actions = await api("/api/ad-control/actions?limit=5").catch(() => ({ items: [] }));
    renderActionList(actions.items || [], $("latestActions"));
  }
  function quickCard(title, href, desc) {
    return `<a class="metric" href="${href}" style="text-decoration:none;color:inherit;"><span>${escapeHtml(desc)}</span><strong style="font-size:18px;">${escapeHtml(title)}</strong></a>`;
  }

  async function renderRules() {
    $("pageRoot").innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <div><h2>规则组管理</h2><span class="hint">只按账号配置。调控对象、命中动作和运行模式彼此独立；新建规则组默认禁用且处于观察模式。</span></div>
          <div class="row"><button class="btn" id="reloadRuleGroupsBtn" type="button">刷新</button><button class="btn primary" id="newRuleGroupBtn" type="button">新建规则组</button></div>
        </div>
        <div class="panel-body">
          <div class="capability-grid">
            <div class="capability-card"><span>复制总熔断</span><strong id="copyFuseState">正式复制未开放</strong><small>复制落表方案待确认，本期 copy 只允许配置、保存和观察，不得产生 Meta 写入。</small></div>
            <div class="capability-card muted"><span>第二阶段</span><strong>广告 Ad 级调控尚未开放</strong><small>Ad 配置可以保存；本期没有 Ad-level insights，立即试算和 runner 会返回 phase_not_enabled。</small></div>
          </div>
          <div class="rule-toolbar account-only-toolbar">
            <div class="field"><label>搜索</label><input id="ruleGroupSearch" placeholder="规则组名称 / ID / 账号 / 剧目" /></div>
            <div class="field"><label>调控对象</label><select id="ruleGroupObjectFilter"><option value="">全部</option><option value="campaign">广告系列 Campaign</option><option value="ad">广告 Ad</option></select></div>
            <div class="field"><label>运行模式</label><select id="ruleGroupModeFilter"><option value="">全部</option><option value="observe">观察模式</option><option value="live">正式执行</option></select></div>
            <div class="field"><label>状态筛选</label><select id="ruleGroupStatusFilter"><option value="">全部</option><option value="enabled">已启用</option><option value="partial">部分启用</option><option value="disabled">已禁用</option><option value="stopped">已急停</option></select></div>
            <div class="field"><label>&nbsp;</label><button class="btn" id="clearRuleGroupFilterBtn" type="button">清空筛选</button></div>
          </div>
          <div class="risk">观察模式会按计划真实扫描和计算“原本会关闭/复制”的对象，但不会调用 Meta 写接口。立即试算是一次性预览，与持续观察模式分开。</div>
          <div class="table-wrap compact-table"><table><thead><tr><th>规则组</th><th>调控对象</th><th>账号范围</th><th>命中动作</th><th>运行与计划</th><th>状态</th><th>操作</th></tr></thead><tbody id="ruleGroupRows"></tbody></table></div>
        </div>
      </section>
      <div class="drawer-overlay hidden" id="ruleGroupDrawer">
        <div class="drawer-panel" role="dialog" aria-modal="true" aria-labelledby="drawerTitle">
          <div class="drawer-head"><div><h2 id="drawerTitle">新建规则组</h2><span class="hint" id="drawerSubTitle"></span></div><button class="btn" id="closeRuleGroupDrawerBtn" type="button">关闭</button></div>
          <div class="drawer-body" id="ruleGroupDrawerBody"></div>
          <div class="drawer-foot">
            <span class="hint" id="drawerSaveHint">新建或编辑后均保持 disabled；需要先试算，再显式启用。</span>
            <div class="row"><button class="btn" id="previewRuleGroupBtn" type="button">立即试算</button><button class="btn primary" id="saveRuleGroupBtn" type="button">保存规则组</button></div>
          </div>
        </div>
      </div>`;
    $("newRuleGroupBtn").onclick = () => openRuleGroupDrawer(null, false);
    $("reloadRuleGroupsBtn").onclick = refreshRuleGroups;
    $("ruleGroupSearch").oninput = renderRuleGroupList;
    $("ruleGroupObjectFilter").onchange = renderRuleGroupList;
    $("ruleGroupModeFilter").onchange = renderRuleGroupList;
    $("ruleGroupStatusFilter").onchange = renderRuleGroupList;
    $("clearRuleGroupFilterBtn").onclick = () => {
      $("ruleGroupSearch").value = "";
      $("ruleGroupObjectFilter").value = "";
      $("ruleGroupModeFilter").value = "";
      $("ruleGroupStatusFilter").value = "";
      renderRuleGroupList();
    };
    $("closeRuleGroupDrawerBtn").onclick = closeRuleGroupDrawer;
    $("saveRuleGroupBtn").onclick = saveFrontendRuleGroup;
    $("previewRuleGroupBtn").onclick = previewDraftRuleGroup;
    await Promise.all([refreshRuleGroups(), refreshCopyCapability()]);
  }

  async function refreshCopyCapability() {
    const node = $("copyFuseState");
    if (!node) return;
    try {
      const status = await api("/api/ad-control/runner/status");
      const capability = status.copy_campaign_enabled ?? status.copy_enabled ?? status.capabilities?.copy_campaign;
      const persistenceReady = status.copy_persistence_ready === true;
      node.textContent = capability === true && persistenceReady ? "已开启" : "正式复制未开放";
      node.className = capability === true && persistenceReady ? "text-ok" : "text-warn";
    } catch (error) {
      node.textContent = "正式复制未开放";
      node.className = "text-warn";
    }
  }

  async function refreshRuleGroups() {
    let data;
    try {
      data = await api("/api/ad-control/rule-groups");
    } catch (error) {
      data = await api("/api/ad-control/bindings");
    }
    state.bindings = data.items || [];
    state.frontendRuleGroups = aggregateRuleGroups(state.bindings);
    renderRuleGroupList();
  }

  function filteredRuleGroups() {
    const query = ($("ruleGroupSearch")?.value || "").trim().toLowerCase();
    const objectLevel = $("ruleGroupObjectFilter")?.value || "";
    const runMode = $("ruleGroupModeFilter")?.value || "";
    const status = $("ruleGroupStatusFilter")?.value || "";
    return state.frontendRuleGroups.filter(group => {
      const haystack = `${group.id} ${group.name} ${(group.account_ids || []).join(" ")} ${(group.country_groups || []).join(" ")} ${JSON.stringify((group.bindings[0] || {}).strategy || {})}`.toLowerCase();
      if (query && !haystack.includes(query)) return false;
      if (objectLevel && group.object_level !== objectLevel) return false;
      if (runMode && group.run_mode !== runMode) return false;
      if (status === "enabled" && !group.enabled) return false;
      if (status === "partial" && !group.partial_enabled) return false;
      if (status === "disabled" && (group.enabled || group.partial_enabled || group.emergency_stopped)) return false;
      if (status === "stopped" && !group.emergency_stopped) return false;
      return true;
    });
  }

  function actionSummary(group) {
    const rules = firstRuleBinding(group)?.rules || [];
    const pauseCount = rules.filter(rule => String(rule.action || "").toLowerCase() === "pause").length;
    const copyCount = rules.filter(rule => String(rule.action || "").toLowerCase() === "copy").length;
    const parts = [];
    if (pauseCount) parts.push(`关闭 ${pauseCount}`);
    if (copyCount) parts.push(`复制 ${copyCount}`);
    return parts.join(" / ") || "--";
  }

  function renderRuleGroupList() {
    const rows = $("ruleGroupRows");
    if (!rows) return;
    const groups = filteredRuleGroups();
    rows.innerHTML = groups.length ? groups.map(group => {
      const stateLabel = group.emergency_stopped
        ? `<span class="badge danger">已急停</span>`
        : (group.enabled
          ? `<span class="badge ok">已启用</span>`
          : (group.partial_enabled
            ? `<span class="badge warn">部分启用 ${group.enabled_count}/${group.target_ids.length || group.bindings.length}</span>`
            : `<span class="badge warn">已禁用</span>`));
      const toggleToEnabled = !group.enabled && !group.partial_enabled;
      const modeClass = group.run_mode === "live" ? "danger" : "ok";
      return `<tr>
        <td><strong>${escapeHtml(group.name)}</strong><div class="mono">${escapeHtml(group.id)}</div><div class="hint">${escapeHtml(group.description || "无说明")}</div></td>
        <td><strong>${escapeHtml(OBJECT_LEVEL_LABELS[group.object_level] || group.object_level)}</strong>${group.object_level === "ad" ? `<div class="hint text-warn">仅保存配置，暂无候选数据</div>` : ""}</td>
        <td><strong>${group.account_count || "--"} 个</strong><div class="hint">${escapeHtml((group.account_ids || []).slice(0, 3).map(id => `act_${id}`).join("、"))}${group.account_count > 3 ? "…" : ""}</div></td>
        <td><strong>${escapeHtml(actionSummary(group))}</strong><div class="hint">${group.rule_count || 0} 条规则</div></td>
        <td><span class="badge ${modeClass}">${escapeHtml(RUN_MODE_LABELS[group.run_mode] || group.run_mode)}</span><div class="hint">${escapeHtml(strategySummary((group.bindings[0] || {}).strategy || group))}</div></td>
        <td>${stateLabel}</td>
        <td><div class="row action-row">
          <button class="btn" data-rule-action="edit" data-group="${escapeHtml(group.id)}" type="button">编辑</button>
          <button class="btn" data-rule-action="duplicate" data-group="${escapeHtml(group.id)}" type="button">复制规则组</button>
          <button class="btn" data-rule-action="preview" data-group="${escapeHtml(group.id)}" type="button">立即试算</button>
          <button class="btn" data-rule-action="logs" data-group="${escapeHtml(group.id)}" type="button">日志</button>
          <button class="btn" data-rule-action="toggle" data-enabled="${toggleToEnabled ? "1" : "0"}" data-group="${escapeHtml(group.id)}" type="button">${group.partial_enabled ? "禁用全部" : (group.enabled ? "禁用" : "启用")}</button>
          <button class="btn danger" data-rule-action="stop" data-group="${escapeHtml(group.id)}" type="button">急停</button>
          <button class="btn danger" data-rule-action="delete" data-group="${escapeHtml(group.id)}" type="button">删除</button>
        </div></td>
      </tr>`;
    }).join("") : `<tr><td colspan="7"><div class="empty">暂无规则组。点击“新建规则组”开始配置。</div></td></tr>`;
    rows.onclick = handleRuleGroupAction;
  }

  function findFrontendRuleGroup(id) {
    return state.frontendRuleGroups.find(group => group.id === id);
  }

  async function handleRuleGroupAction(event) {
    const button = event.target.closest("[data-rule-action]");
    if (!button) return;
    const group = findFrontendRuleGroup(button.dataset.group);
    if (!group) return toast("规则组不存在，请刷新", "error");
    try {
      if (button.dataset.ruleAction === "edit") return await openRuleGroupDrawer(group, false);
      if (button.dataset.ruleAction === "duplicate") return await openRuleGroupDrawer(group, true);
      if (button.dataset.ruleAction === "preview") return await previewFrontendRuleGroup(group);
      if (button.dataset.ruleAction === "logs") return openRuleGroupLogs(group);
      if (button.dataset.ruleAction === "toggle") return await setFrontendRuleGroupEnabled(group, button.dataset.enabled === "1");
      if (button.dataset.ruleAction === "stop") return await emergencyStopFrontendRuleGroup(group);
      if (button.dataset.ruleAction === "delete") return await deleteFrontendRuleGroup(group);
    } catch (error) {
      toast(error.message || String(error), "error");
    }
  }

  function firstRuleBinding(group) {
    return (group?.bindings || []).find(item => Array.isArray(item.rules) && item.rules.length) || (group?.bindings || [])[0] || null;
  }

  function normalizedRules(rules) {
    return (Array.isArray(rules) && rules.length ? rules : defaultRules).map(rule => {
      const item = Object.assign({}, rule);
      const action = String(item.action || "").trim().toLowerCase();
      item.action = action === "observe" ? "pause" : action;
      item.enabled = item.enabled !== false;
      return item;
    });
  }

  function copyDraftConfig(raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    const budget = raw.budget && typeof raw.budget === "object" ? raw.budget : raw;
    let mode = String(budget.type || budget.mode || raw.budget_strategy || "actual_cpi_multiplier").toLowerCase();
    if (mode === "cpi_multiple") mode = "actual_cpi_multiplier";
    if (mode === "x_cpi") mode = (budget.fixed_cpi || budget.target_cpi) ? "fixed_target_cpi_multiplier" : "actual_cpi_multiplier";
    if (mode === "source_ratio") mode = "source_budget_ratio";
    const roas = raw.roas_bid && typeof raw.roas_bid === "object"
      ? raw.roas_bid
      : (raw.roas && typeof raw.roas === "object" ? raw.roas : raw);
    return {
      budget_strategy: mode,
      cpi_multiplier: Number(budget.multiplier || budget.x || raw.cpi_multiplier || raw.cpi_multiple || 10),
      fixed_target_cpi: Number(budget.target_cpi || budget.fixed_cpi || raw.fixed_target_cpi || 1),
      source_budget_ratio: Number(budget.ratio || budget.source_ratio || raw.source_budget_ratio || 0.5),
      roas_direction: roas.direction || raw.roas_direction || "decrease",
      roas_percent: Number(roas.percent ?? raw.roas_percent ?? 10),
    };
  }

  function buildRuleGroupDraft(group, duplicate) {
    const first = group ? firstRuleBinding(group) : null;
    const strategy = (first && first.strategy) || {};
    const rules = normalizedRules(first?.rules);
    const legacyObserve = Array.isArray(first?.rules) && first.rules.some(rule => String(rule.action || "").toLowerCase() === "observe");
    const unknownActions = Array.from(new Set(rules.map(rule => rule.action).filter(action => !["pause", "copy"].includes(action))));
    const accountIds = group?.account_ids?.length ? group.account_ids : (strategy.selected_account_ids || first?.account_ids || []);
    const schedule = strategy.schedule || {};
    const limits = strategy.limits || {};
    const dramaScope = strategy.drama_scope || {};
    const firstCopyRule = rules.find(rule => rule.action === "copy") || {};
    const copyConfig = copyDraftConfig(firstCopyRule.copy || firstCopyRule.copy_config || strategy.copy || strategy.copy_config || {});
    const ruleDramaScope = firstCopyRule.drama_scope || dramaScope;
    const groupCopy = strategy.copy && typeof strategy.copy === "object" ? strategy.copy : {};
    const candidateSelection = firstCopyRule.candidate_selection || strategy.candidate_selection || groupCopy.candidate_selection || {};
    const candidateSelectionMode = candidateSelection.mode || "top_n_per_account";
    const id = group && !duplicate ? group.id : newFrontendRuleGroupId();
    const sourceTargetIds = group ? ruleGroupTargetIds(group) : [];
    return {
      mode: group && !duplicate ? "edit" : "create",
      id,
      sourceGroup: group || null,
      migrate_from_group_ids: group && !duplicate ? sourceTargetIds.filter(targetId => targetId !== id) : [],
      legacy_observe_migrated: legacyObserve,
      unknown_actions: unknownActions,
      name: group ? `${group.name}${duplicate ? " 副本" : ""}` : "账号自动调控规则",
      description: group ? (group.description || strategy.description || "") : "按账号扫描广告系列，命中后关闭或复制；默认观察且禁用。",
      object_level: first?.object_level || strategy.object_level || strategy.target_level || group?.object_level || "campaign",
      run_mode: !group || duplicate ? "observe" : (legacyObserve ? "observe" : (first?.run_mode || strategy.run_mode || strategy.execution_mode || group?.run_mode || "observe")),
      rules,
      default_window: first?.rule_set_default_window || { type: "since_start", hours: 24 },
      selectedAccountKeys: new Set((accountIds || []).map(normalizeAccountId).filter(Boolean)),
      schedule: {
        type: schedule.type || "fixed_time",
        time: schedule.time || strategy.close_time || "10:00",
        interval_minutes: Number(schedule.interval_minutes || 60),
        allowed_start: schedule.allowed_start || "00:00",
        allowed_end: schedule.allowed_end || schedule.execute_before || "23:00",
        timezone: "account",
      },
      limits: {
        rule_daily_limit: Number(limits.rule_daily_limit || 1),
        user_daily_limit: Number(limits.user_daily_limit || 10),
        source_cooldown_days: Number(limits.source_cooldown_days || 1),
      },
      drama_scope: {
        type: ruleDramaScope.type || "all",
        recent_days: Number(ruleDramaScope.recent_days || 7),
        drama_ids: Array.isArray(ruleDramaScope.drama_ids) ? ruleDramaScope.drama_ids : [],
      },
      candidate_selection_mode: candidateSelectionMode,
      top_n_per_account: Number(firstCopyRule.top_n_per_account || candidateSelection.top_n || strategy.top_n_per_account || groupCopy.top_n_per_account || 1),
      copy_config: copyConfig,
    };
  }

  async function openRuleGroupDrawer(group, duplicate) {
    state.ruleGroupDraft = buildRuleGroupDraft(group, duplicate);
    $("ruleGroupDrawer").classList.remove("hidden");
    renderRuleGroupDrawer();
    await ensureRuleGroupAccounts();
    renderRuleGroupAccounts();
    updateRuleGroupSummary();
  }

  function closeRuleGroupDrawer() {
    state.ruleGroupDraft = null;
    $("ruleGroupDrawer").classList.add("hidden");
  }

  function renderRuleGroupDrawer() {
    const draft = state.ruleGroupDraft;
    const builderRule = primaryRuleFromDraft(draft);
    const copyRules = (draft.rules || []).filter(rule => String(rule?.action || "").toLowerCase() === "copy");
    const editableCopyRule = copyRules[0] || {};
    const builderRuleName = builderRule.name || "账号自动调控规则";
    const builderAction = ["pause", "copy"].includes(builderRule.action) ? builderRule.action : "";
    const builderTimezones = conditionList(builderRule, ["account_time_zone", "time_zone", "timezone", "account_timezone"], ["in"], []);
    const builderLanguages = conditionList(builderRule, "language", ["not_in"], []);
    const builderCountries = conditionList(builderRule, ["country", "country_group", "geo", "region"], ["in"], []);
    const builderAgeHours = conditionNumber(builderRule, "age_hours", ["gte"], "");
    const builderSpendMin = conditionNumber(builderRule, "spend", ["gte", "gt"], "");
    const builderInstallMax = conditionNumber(builderRule, "install", ["lte", "lt"], "");
    const builderRoasMax = conditionNumber(builderRule, "roas_pct", ["lte", "lt"], "");
    const builderPurchaseMax = conditionNumber(builderRule, "purchase", ["lte", "lt"], "");
    const builderCpaMin = conditionNumber(builderRule, "purchase_cpa", ["gte", "gt"], "");
    $("drawerTitle").textContent = draft.mode === "edit" ? "编辑规则组" : "新建规则组";
    $("drawerSubTitle").textContent = draft.mode === "edit" ? `正在编辑 ${draft.id}` : "账号是唯一业务范围；保存后默认 disabled + 观察模式";
    $("ruleGroupDrawerBody").innerHTML = `
      <section class="drawer-section">
        <div class="section-title"><span>1</span><h3>基础信息与运行模式</h3></div>
        <div class="grid two"><div class="field"><label>规则组名称</label><input id="drawerGroupName" value="${escapeHtml(draft.name)}" /></div><div class="field"><label>规则组 ID</label><input id="drawerGroupId" value="${escapeHtml(draft.id)}" readonly /></div></div>
        <div class="field"><label>规则组说明</label><textarea id="drawerGroupDescription" class="short-textarea">${escapeHtml(draft.description)}</textarea></div>
        <div class="grid two"><div class="field"><label>调控对象</label><select id="drawerObjectLevel"><option value="campaign">广告系列 Campaign</option><option value="ad">广告 Ad（第二阶段仅保存）</option></select><span class="hint">调控对象决定筛选和关闭/复制发生在哪一层，不是规则动作。</span></div><div class="field"><label>运行模式</label><div class="mode-options"><label class="mode-option"><input type="radio" name="drawerRunMode" value="observe" ${draft.run_mode !== "live" ? "checked" : ""} /><span><strong>观察模式</strong><small>Campaign 会扫描；Ad 本期只保存配置</small></span></label><label class="mode-option danger-option"><input type="radio" name="drawerRunMode" value="live" ${draft.run_mode === "live" ? "checked" : ""} ${draft.mode === "create" ? "disabled" : ""} /><span><strong>正式执行</strong><small>${draft.mode === "create" ? "新组先保存为观察，再单独切换" : "启用时需要再次确认"}</small></span></label></div></div></div>
        ${draft.legacy_observe_migrated ? `<div class="risk compact-risk">检测到旧规则动作 observe：本次保存会显式迁移为运行模式 observe + 命中动作 pause；迁移后仍保持禁用。</div>` : ""}
        ${draft.unknown_actions.length ? `<div class="risk compact-risk">检测到未知 action：${escapeHtml(draft.unknown_actions.join("、") || "空值")}。不会自动改成关闭，请在规则 JSON 中明确改为 pause 或 copy 后再保存。</div>` : ""}
        <div class="risk compact-risk hidden" id="adPhaseNotice">Ad 属于第二阶段：本期没有 Ad-level insights，仅允许保存配置；立即试算和 runner 会返回 phase_not_enabled，不会展示 Ad 候选。</div>
        <div class="risk compact-risk">新规则组即使选择“正式执行”，保存后仍为禁用；正式启用需要二次确认。复制熔断未开启时，后端必须拒绝复制写入。</div>
      </section>
      <section class="drawer-section">
        <div class="section-title"><span>2</span><h3>账号范围</h3></div>
        <div class="grid"><div class="field"><label>账号搜索</label><input id="drawerAccountSearch" placeholder="搜索账号名 / ID；输入 act_... 可直接加入" /></div><div class="field"><label>时区筛选</label><input id="drawerTimezoneFilter" placeholder="+8 / UTC+8" /></div><div class="field"><label>&nbsp;</label><label class="check-inline"><input id="drawerPlus8Only" type="checkbox" /> 只看 +8</label></div><div class="field"><label>&nbsp;</label><div class="row"><button class="btn" id="drawerAddSearchAccount" type="button">加入搜索账号</button><button class="btn" id="drawerSelectVisibleAccounts" type="button">全选可见</button><button class="btn" id="drawerClearVisibleAccounts" type="button">清空可见</button></div></div></div>
        <div class="manual-account-box"><div class="field"><label>手动添加账号 ID</label><textarea id="drawerManualAccounts" class="short-textarea" placeholder="支持 act_1146901540906487 或裸 ID；多个账号用换行、逗号或空格分隔"></textarea><span class="hint">未知账号也可以保存请求，由后端校验账号、渠道和 Token；前端不会推测产品归属。</span></div><button class="btn" id="drawerAddManualAccounts" type="button">加入账号</button></div>
        <div class="selected-account-box"><div class="bulk-head"><strong>已选账号</strong><span class="hint" id="drawerSelectedAccountHint">0 个</span></div><div class="selected-account-list" id="drawerSelectedAccounts"></div></div>
        <div class="account-product-block"><div class="bulk-head"><strong>可选账号</strong><span class="hint" id="drawerAvailableAccountHint">加载中...</span></div><div class="account-list" id="drawerAccounts"></div></div>
      </section>
      <section class="drawer-section">
        <div class="section-title"><span>3</span><h3>规则条件与命中动作</h3></div>
        <div class="grid"><div class="field"><label>规则名称</label><input id="builderRuleName" value="${escapeHtml(builderRuleName)}" /></div><div class="field"><label>命中动作</label><select id="builderAction"><option value="">请选择动作</option><option value="pause">关闭</option><option value="copy">复制</option></select><span class="hint">观察不再是动作，由规则组顶部运行模式控制。</span></div><div class="field"><label>账户时区 in</label><input id="builderTimezones" value="${escapeHtml(builderTimezones.join(","))}" placeholder="留空=不限" /></div><div class="field"><label>国家组 in</label><input id="builderCountries" value="${escapeHtml(builderCountries.join(","))}" placeholder="留空=不限" /></div></div>
        <div class="grid"><div class="field"><label>排除语种 not in</label><input id="builderLanguages" value="${escapeHtml(builderLanguages.join(","))}" placeholder="留空=不限" /></div><div class="field"><label>已运行小时 >=</label><input id="builderAgeHours" type="number" min="0" placeholder="可选" value="${escapeHtml(builderAgeHours)}" /></div><div class="field"><label>消耗 >=</label><input id="builderSpendMin" type="number" min="0" step="0.01" placeholder="可选" value="${escapeHtml(builderSpendMin)}" /></div><div class="field"><label>安装 <=</label><input id="builderInstallMax" type="number" min="0" placeholder="可选" value="${escapeHtml(builderInstallMax)}" /></div></div>
        <div class="grid"><div class="field"><label>ROAS% <=</label><input id="builderRoasMax" type="number" min="0" step="0.01" placeholder="可选" value="${escapeHtml(builderRoasMax)}" /></div><div class="field"><label>购物 <=</label><input id="builderPurchaseMax" type="number" min="0" placeholder="可选" value="${escapeHtml(builderPurchaseMax)}" /></div><div class="field"><label>Purchase CPA >=</label><input id="builderCpaMin" type="number" min="0" step="0.01" placeholder="可选" value="${escapeHtml(builderCpaMin)}" /></div><div class="field"><label>指标窗口</label><select id="drawerWindowType"><option value="since_start">起始至当前</option><option value="today">账户当天</option><option value="recent_hours">最近 N 小时</option></select></div></div>
        <div class="grid single"><div class="field"><label>N 小时</label><input id="drawerWindowHours" type="number" min="1" max="720" value="${escapeHtml(draft.default_window.hours || 24)}" /></div></div>
        <div class="row"><button class="btn" id="buildDrawerRuleBtn" type="button">按上方配置生成规则 JSON</button><span class="hint">同一对象命中多条规则时关闭优先；同动作再按 priority，只执行一条，其余记录为 shadowed。</span></div>
        <div class="field"><label>高级规则 JSON</label><textarea id="drawerRulesJson">${escapeHtml(JSON.stringify(draft.rules, null, 2))}</textarea><span class="hint">action 仅允许 pause 或 copy；运行模式不写入单条规则。生成按钮只更新第一条主规则，不会删除后续规则。</span></div>
      </section>
      <section class="drawer-section">
        <div class="section-title"><span>4</span><h3>执行时间与额度</h3></div>
        <div class="grid"><div class="field"><label>执行计划</label><select id="drawerScheduleType"><option value="fixed_time">每天固定时间</option><option value="interval">间隔执行</option></select></div><div class="field" id="drawerFixedTimeField"><label>每天执行时间</label><input id="drawerExecuteTime" type="time" value="${escapeHtml(draft.schedule.time)}" /></div><div class="field hidden" id="drawerIntervalField"><label>间隔分钟</label><input id="drawerIntervalMinutes" type="number" min="5" step="5" value="${escapeHtml(draft.schedule.interval_minutes)}" /></div><div class="field"><label>允许开始时间</label><input id="drawerAllowedStart" type="time" value="${escapeHtml(draft.schedule.allowed_start)}" /></div><div class="field"><label>允许截止时间</label><input id="drawerExecuteBefore" type="time" value="${escapeHtml(draft.schedule.allowed_end)}" /></div><div class="field"><label>执行时区</label><input value="目标广告账号时区" disabled /></div></div>
        <div class="grid three hidden" id="drawerCopyLimits"><div class="field"><label>单规则每天最多复制</label><input id="drawerRuleDailyLimit" type="number" min="1" max="100" value="${escapeHtml(draft.limits.rule_daily_limit)}" /></div><div class="field"><label>当前用户每天最多复制</label><input id="drawerUserDailyLimit" type="number" min="1" max="500" value="${escapeHtml(draft.limits.user_daily_limit)}" /></div><div class="field"><label>同一来源冷却天数</label><input id="drawerSourceCooldown" type="number" min="0" max="30" value="${escapeHtml(draft.limits.source_cooldown_days)}" /></div></div>
      </section>
      <section class="drawer-section hidden" id="copyScopePanel">
        <div class="section-title"><span>5</span><h3>剧目与排名范围</h3></div>
        <div class="grid"><div class="field"><label>剧目范围</label><select id="drawerDramaScope"><option value="all">全部剧</option><option value="recent_days">最近 X 天的剧</option><option value="specified">指定剧</option></select></div><div class="field" id="drawerDramaDaysField"><label>最近天数</label><input id="drawerDramaDays" type="number" min="1" max="365" value="${escapeHtml(draft.drama_scope.recent_days)}" /></div><div class="field hidden" id="drawerDramaIdsField"><label>指定剧 ID / series_code</label><textarea id="drawerDramaIds" class="short-textarea" placeholder="多个值用换行、逗号或空格分隔">${escapeHtml(draft.drama_scope.drama_ids.join("\n"))}</textarea></div><div class="field"><label>候选选择</label><select id="drawerCandidateSelectionMode"><option value="all">全部符合条件</option><option value="top_n_per_account">每账号 Top N</option></select></div><div class="field" id="drawerTopNField"><label>每账号 Top N</label><input id="drawerTopN" type="number" min="1" max="50" value="${escapeHtml(draft.top_n_per_account)}" /><span class="hint">仅 Top N 模式生效，默认按 ROAS、消耗、对象 ID 稳定排序。</span></div></div>
      </section>
      <section class="drawer-section hidden" id="copyConfigPanel">
        <div class="section-title"><span>6</span><h3>复制后预算与 ROAS</h3></div>
        <div class="risk compact-risk">复制落表方案待确认。本期可配置、保存和观察 copy；正式复制后端会 fail-closed，不会调用 Meta copy。</div>
        ${copyRules.length > 1 ? `<div class="risk compact-risk">当前有 ${copyRules.length} 条 copy 规则。本区表单只编辑第一条 copy 规则“${escapeHtml(editableCopyRule.name || editableCopyRule.rule_id || "未命名")}”；其他 copy 规则的候选、剧目、预算和 ROAS 会原样保留，请在“高级规则 JSON”中单独维护。</div>` : ""}
        <div class="grid"><div class="field"><label>预算策略</label><select id="drawerBudgetStrategy"><option value="actual_cpi_multiplier">实际 CPI × 倍数</option><option value="fixed_target_cpi_multiplier">固定目标 CPI × 倍数</option><option value="source_budget_ratio">来源预算 × 比例</option></select></div><div class="field" id="drawerCpiMultipleField"><label>CPI 倍数 X</label><input id="drawerCpiMultiple" type="number" min="0.01" step="0.01" value="${escapeHtml(draft.copy_config.cpi_multiplier)}" /></div><div class="field hidden" id="drawerFixedTargetCpiField"><label>固定目标 CPI</label><input id="drawerFixedTargetCpi" type="number" min="0.01" step="0.01" value="${escapeHtml(draft.copy_config.fixed_target_cpi)}" /></div><div class="field hidden" id="drawerBudgetRatioField"><label>来源预算比例</label><input id="drawerBudgetRatio" type="number" min="0.01" max="10" step="0.01" value="${escapeHtml(draft.copy_config.source_budget_ratio)}" /></div><div class="field"><label>ROAS 出价调整</label><select id="drawerRoasDirection"><option value="increase">提高</option><option value="decrease">降低</option></select></div><div class="field"><label>调整百分比</label><input id="drawerRoasPercent" type="number" min="0" max="100" step="0.1" value="${escapeHtml(draft.copy_config.roas_percent)}" /></div></div>
      </section>
      <section class="drawer-section">
        <div class="section-title"><span>7</span><h3>保存前摘要</h3></div>
        <div class="summary-box" id="drawerSummary"></div>
      </section>`;
    $("drawerObjectLevel").value = draft.object_level;
    $("drawerWindowType").value = draft.default_window.type || "since_start";
    $("builderAction").value = builderAction;
    $("drawerScheduleType").value = draft.schedule.type;
    $("drawerDramaScope").value = draft.drama_scope.type;
    $("drawerCandidateSelectionMode").value = draft.candidate_selection_mode;
    $("drawerBudgetStrategy").value = draft.copy_config.budget_strategy;
    $("drawerRoasDirection").value = draft.copy_config.roas_direction;
    bindRuleGroupDrawerEvents();
    updateRuleGroupConditionalFields();
  }

  function bindRuleGroupDrawerEvents() {
    $("drawerAccountSearch").oninput = renderRuleGroupAccounts;
    $("drawerAccountSearch").onkeydown = event => {
      if (event.key === "Enter") {
        event.preventDefault();
        addSearchRuleAccounts();
      }
    };
    $("drawerTimezoneFilter").oninput = renderRuleGroupAccounts;
    $("drawerPlus8Only").onchange = renderRuleGroupAccounts;
    $("drawerAddSearchAccount").onclick = addSearchRuleAccounts;
    $("drawerSelectVisibleAccounts").onclick = () => setVisibleRuleAccounts(true);
    $("drawerClearVisibleAccounts").onclick = () => setVisibleRuleAccounts(false);
    $("drawerAddManualAccounts").onclick = addManualRuleAccounts;
    $("drawerAccounts").onchange = event => {
      const input = event.target.closest("[data-rule-account]");
      if (!input) return;
      if (input.checked) state.ruleGroupDraft.selectedAccountKeys.add(input.dataset.accountKey);
      else state.ruleGroupDraft.selectedAccountKeys.delete(input.dataset.accountKey);
      renderSelectedRuleAccounts();
      updateRuleGroupSummary();
    };
    $("drawerSelectedAccounts").onclick = event => {
      const button = event.target.closest("[data-remove-account-key]");
      if (!button) return;
      state.ruleGroupDraft.selectedAccountKeys.delete(button.dataset.removeAccountKey);
      renderRuleGroupAccounts();
    };
    $("buildDrawerRuleBtn").onclick = () => {
      try {
        const existingRules = JSON.parse($("drawerRulesJson").value || "[]");
        if (!Array.isArray(existingRules)) throw new Error("规则 JSON 必须是数组");
        const generatedRule = buildDrawerRulesFromThresholds()[0];
        $("drawerRulesJson").value = JSON.stringify(mergeGeneratedPrimaryRule(existingRules, generatedRule), null, 2);
        updateRuleGroupConditionalFields();
        updateRuleGroupSummary();
        toast("已更新第一条主规则，其他规则保持不变");
      } catch (error) {
        toast(error.message || String(error), "error");
      }
    };
    $("builderAction").onchange = () => {
      updateRuleGroupConditionalFields();
      updateRuleGroupSummary();
    };
    $("drawerObjectLevel").onchange = () => {
      updateRuleGroupConditionalFields();
      updateRuleGroupSummary();
    };
    $("drawerScheduleType").onchange = () => { updateRuleGroupConditionalFields(); updateRuleGroupSummary(); };
    $("drawerDramaScope").onchange = () => { updateRuleGroupConditionalFields(); updateRuleGroupSummary(); };
    $("drawerCandidateSelectionMode").onchange = () => { updateRuleGroupConditionalFields(); updateRuleGroupSummary(); };
    $("drawerBudgetStrategy").onchange = () => { updateRuleGroupConditionalFields(); updateRuleGroupSummary(); };
    $("drawerRulesJson").onblur = () => { updateRuleGroupConditionalFields(); updateRuleGroupSummary(); };
    document.querySelectorAll('input[name="drawerRunMode"]').forEach(input => {
      input.onchange = () => {
        if (input.value === "live" && input.checked && !confirm("正式执行会在启用后允许 pause 写动作；copy 落表未确认时仍会被后端阻断。当前只是在配置模式，保存后保持禁用，确认选择？")) {
          document.querySelector('input[name="drawerRunMode"][value="observe"]').checked = true;
        } else if (input.value === "live" && input.checked) {
          state.ruleGroupDraft.liveModeConfirmed = true;
        }
        updateRuleGroupSummary();
      };
    });
    $("ruleGroupDrawerBody").oninput = event => {
      if (event.target.id !== "drawerRulesJson") updateRuleGroupSummary();
    };
  }

  function updateRuleGroupConditionalFields() {
    const action = $("builderAction")?.value || "pause";
    let hasCopy = action === "copy";
    try {
      hasCopy = hasCopy || JSON.parse($("drawerRulesJson")?.value || "[]").some(rule => String(rule?.action || "").toLowerCase() === "copy");
    } catch (error) {}
    $("copyConfigPanel")?.classList.toggle("hidden", !hasCopy);
    $("copyScopePanel")?.classList.toggle("hidden", !hasCopy);
    $("drawerCopyLimits")?.classList.toggle("hidden", !hasCopy);
    const scheduleType = $("drawerScheduleType")?.value || "fixed_time";
    $("drawerFixedTimeField")?.classList.toggle("hidden", scheduleType !== "fixed_time");
    $("drawerIntervalField")?.classList.toggle("hidden", scheduleType !== "interval");
    const dramaType = $("drawerDramaScope")?.value || "all";
    $("drawerDramaDaysField")?.classList.toggle("hidden", dramaType !== "recent_days");
    $("drawerDramaIdsField")?.classList.toggle("hidden", dramaType !== "specified");
    const candidateSelectionMode = $("drawerCandidateSelectionMode")?.value || "top_n_per_account";
    $("drawerTopNField")?.classList.toggle("hidden", candidateSelectionMode !== "top_n_per_account");
    const budgetType = $("drawerBudgetStrategy")?.value || "actual_cpi_multiplier";
    $("drawerCpiMultipleField")?.classList.toggle("hidden", budgetType === "source_budget_ratio");
    $("drawerFixedTargetCpiField")?.classList.toggle("hidden", budgetType !== "fixed_target_cpi_multiplier");
    $("drawerBudgetRatioField")?.classList.toggle("hidden", budgetType !== "source_budget_ratio");
    const isAd = $("drawerObjectLevel")?.value === "ad";
    $("adPhaseNotice")?.classList.toggle("hidden", !isAd);
    const liveInput = document.querySelector('input[name="drawerRunMode"][value="live"]');
    if (liveInput) {
      liveInput.disabled = state.ruleGroupDraft?.mode === "create" || isAd;
      if (isAd && liveInput.checked) {
        document.querySelector('input[name="drawerRunMode"][value="observe"]').checked = true;
      }
    }
  }

  function mergeRuleGroupAccounts(groups) {
    const map = new Map();
    (groups || []).flat().forEach(item => {
      const id = accountValue(item);
      if (!id) return;
      const existing = map.get(id) || {};
      map.set(id, Object.assign({}, existing, item, {
        account_id: id,
        account_name: item.account_name || item.name || existing.account_name || existing.name || id,
        time_zone: item.time_zone || existing.time_zone || "",
      }));
    });
    return Array.from(map.values()).sort((a, b) => String(a.account_name || a.account_id).localeCompare(String(b.account_name || b.account_id), "zh-CN"));
  }

  async function ensureRuleGroupAccounts() {
    try {
      const data = await api("/api/ad-control/accounts");
      state.ruleGroupAccounts = mergeRuleGroupAccounts([data.items || []]);
      return;
    } catch (error) {
      const legacy = await Promise.all(ALLOWED_PRODUCTS.map(value => api(`/api/ad-control/accounts?product=${encodeURIComponent(value)}`).catch(() => ({ items: [] }))));
      state.ruleGroupAccounts = mergeRuleGroupAccounts(legacy.map(item => item.items || []));
    }
  }

  function captureDraftSelectedAccounts() {
    const draft = state.ruleGroupDraft;
    if (!draft) return;
    document.querySelectorAll("[data-rule-account]").forEach(input => {
      if (input.checked) draft.selectedAccountKeys.add(input.dataset.accountKey);
      else draft.selectedAccountKeys.delete(input.dataset.accountKey);
    });
  }

  function accountValue(item) {
    return normalizeAccountId((item && (item.account_id || item.ad_account_id)) || "");
  }

  function normalizeAccountId(value) {
    return String(value || "").trim().replace(/^act_/i, "");
  }

  function ruleAccountVisible(account) {
    const query = ($("drawerAccountSearch")?.value || "").trim().toLowerCase();
    const tzFilter = ($("drawerTimezoneFilter")?.value || "").trim();
    const plus8Only = $("drawerPlus8Only")?.checked;
    const haystack = `${accountValue(account)} ${account.account_name || account.name || ""}`.toLowerCase();
    if (query && !haystack.includes(normalizeAccountId(query).toLowerCase()) && !haystack.includes(query)) return false;
    if (plus8Only && !isPlus8Timezone(account.time_zone)) return false;
    if (tzFilter && !Array.from(timezoneValues(account.time_zone)).some(item => timezoneValues(tzFilter).has(item))) return false;
    return true;
  }

  function renderRuleGroupAccounts() {
    const root = $("drawerAccounts");
    const draft = state.ruleGroupDraft;
    if (!root || !draft) return;
    captureDraftSelectedAccounts();
    const visible = state.ruleGroupAccounts.filter(ruleAccountVisible);
    $("drawerAvailableAccountHint").textContent = `${visible.length}/${state.ruleGroupAccounts.length} 个账号`;
    root.innerHTML = visible.length ? visible.map(account => {
      const id = accountValue(account);
      const checked = draft.selectedAccountKeys.has(id) ? "checked" : "";
      return `<label class="account-option"><input type="checkbox" data-rule-account="1" data-account-key="${escapeHtml(id)}" value="${escapeHtml(id)}" ${checked} /><div><div class="account-title">${escapeHtml(account.account_name || account.name || id)}</div><div class="account-meta">act_${escapeHtml(id)} / ${escapeHtml(account.time_zone || "--")}</div></div></label>`;
    }).join("") : `<div class="empty">无匹配账号；可以手动添加 account_id。</div>`;
    renderSelectedRuleAccounts();
    updateRuleGroupSummary();
  }

  function addManualRuleAccounts() {
    const ids = Array.from(new Set(splitValues($("drawerManualAccounts").value).map(normalizeAccountId).filter(value => /^\d+$/.test(value))));
    if (!ids.length) return toast("请粘贴有效的数字账号 ID", "error");
    ids.forEach(id => state.ruleGroupDraft.selectedAccountKeys.add(id));
    $("drawerManualAccounts").value = "";
    renderRuleGroupAccounts();
    toast(`已加入 ${ids.length} 个账号`);
  }

  function addSearchRuleAccounts() {
    const ids = Array.from(new Set(splitValues($("drawerAccountSearch").value).map(normalizeAccountId).filter(value => /^\d+$/.test(value))));
    if (!ids.length) return toast("请在搜索框输入有效的 account_id", "error");
    ids.forEach(id => state.ruleGroupDraft.selectedAccountKeys.add(id));
    renderRuleGroupAccounts();
    toast(`已从搜索框加入 ${ids.length} 个账号`);
  }

  function selectedAccountDisplay(accountId) {
    const account = state.ruleGroupAccounts.find(item => accountValue(item) === accountId);
    if (!account) return { title: accountId, meta: "手动添加，等待后端校验" };
    return {
      title: account.account_name || account.name || accountId,
      meta: `act_${accountId} / ${account.time_zone || "--"}`,
    };
  }

  function renderSelectedRuleAccounts() {
    const root = $("drawerSelectedAccounts");
    const hint = $("drawerSelectedAccountHint");
    const draft = state.ruleGroupDraft;
    if (!root || !draft) return;
    const selected = Array.from(draft.selectedAccountKeys).sort();
    if (hint) hint.textContent = `${selected.length} 个`;
    root.innerHTML = selected.length ? selected.map(accountId => {
      const display = selectedAccountDisplay(accountId);
      return `<div class="selected-account-item"><div><span>${escapeHtml(display.title)}</span><small>${escapeHtml(display.meta)}</small></div><button class="btn" data-remove-account-key="${escapeHtml(accountId)}" type="button">移除</button></div>`;
    }).join("") : `<div class="empty compact-empty">暂无已选账号，可从下方列表勾选或手动粘贴 account_id。</div>`;
  }

  function setVisibleRuleAccounts(checked) {
    document.querySelectorAll("[data-rule-account]").forEach(input => {
      input.checked = checked;
      if (checked) state.ruleGroupDraft.selectedAccountKeys.add(input.dataset.accountKey);
      else state.ruleGroupDraft.selectedAccountKeys.delete(input.dataset.accountKey);
    });
    renderSelectedRuleAccounts();
    updateRuleGroupSummary();
  }

  function copyConfigFromDrawer() {
    return {
      budget_strategy: $("drawerBudgetStrategy")?.value || "actual_cpi_multiplier",
      cpi_multiplier: Number($("drawerCpiMultiple")?.value || 0),
      fixed_target_cpi: Number($("drawerFixedTargetCpi")?.value || 0),
      source_budget_ratio: Number($("drawerBudgetRatio")?.value || 0),
      roas_direction: $("drawerRoasDirection")?.value || "decrease",
      roas_percent: Number($("drawerRoasPercent")?.value || 0),
    };
  }

  function copyPayloadFromDrawer() {
    const values = copyConfigFromDrawer();
    const budget = {
      type: values.budget_strategy,
      mode: values.budget_strategy,
      multiplier: values.cpi_multiplier,
    };
    if (values.budget_strategy === "fixed_target_cpi_multiplier") {
      budget.target_cpi = values.fixed_target_cpi;
      budget.fixed_cpi = values.fixed_target_cpi;
    }
    if (values.budget_strategy === "source_budget_ratio") {
      budget.ratio = values.source_budget_ratio;
      budget.source_ratio = values.source_budget_ratio;
    }
    const roas = {
      direction: values.roas_direction,
      percent: values.roas_percent,
    };
    return {
      budget,
      roas_bid: Object.assign({}, roas),
      roas: Object.assign({}, roas),
      final_status: "ACTIVE",
    };
  }

  function mergeRuleCopy(defaultCopy, overrideCopy) {
    const override = overrideCopy && typeof overrideCopy === "object" ? overrideCopy : {};
    return Object.assign({}, defaultCopy, override, {
      budget: Object.assign({}, defaultCopy.budget || {}, override.budget || {}),
      roas_bid: Object.assign({}, defaultCopy.roas_bid || {}, override.roas_bid || {}),
      roas: Object.assign({}, defaultCopy.roas || {}, override.roas || {}),
    });
  }

  function dramaScopeFromDrawer() {
    const days = Number($("drawerDramaDays")?.value || 0);
    return {
      type: $("drawerDramaScope")?.value || "all",
      days,
      recent_days: days,
      drama_ids: splitValues($("drawerDramaIds")?.value),
    };
  }

  function candidateSelectionFromDrawer() {
    const mode = $("drawerCandidateSelectionMode")?.value || "top_n_per_account";
    return {
      mode,
      top_n: mode === "top_n_per_account" ? Number($("drawerTopN")?.value || 0) : 0,
    };
  }

  function buildDrawerRulesFromThresholds() {
    const conditions = [];
    const timezones = splitValues($("builderTimezones").value);
    const languages = splitValues($("builderLanguages").value);
    const countries = splitValues($("builderCountries").value);
    if (timezones.length) conditions.push({ field: "account_time_zone", op: "in", value: timezones });
    if (languages.length) conditions.push({ field: "language", op: "not_in", value: languages });
    if (countries.length) conditions.push({ field: "country", op: "in", value: countries });
    const addNumber = (id, field, op) => {
      if ($(id).value !== "") conditions.push({ field, op, value: Number($(id).value) });
    };
    addNumber("builderAgeHours", "age_hours", "gte");
    addNumber("builderSpendMin", "spend", "gte");
    addNumber("builderInstallMax", "install", "lte");
    addNumber("builderRoasMax", "roas_pct", "lte");
    addNumber("builderPurchaseMax", "purchase", "lte");
    addNumber("builderCpaMin", "purchase_cpa", "gte");
    const action = $("builderAction").value;
    if (!["pause", "copy"].includes(action)) throw new Error("请明确选择关闭或复制动作");
    const rule = {
      name: $("builderRuleName").value.trim() || "账号自动调控规则",
      action,
      enabled: true,
      level: $("drawerObjectLevel").value || "campaign",
      window: { type: $("drawerWindowType").value || "since_start" },
      conditions,
    };
    if (action === "copy") {
      const candidateSelection = candidateSelectionFromDrawer();
      rule.copy = copyPayloadFromDrawer();
      rule.drama_scope = dramaScopeFromDrawer();
      rule.candidate_selection = candidateSelection;
      rule.top_n_per_account = candidateSelection.mode === "top_n_per_account" ? candidateSelection.top_n : 0;
    }
    return [rule];
  }

  function readRuleGroupDraftFromDrawer() {
    const draft = state.ruleGroupDraft;
    captureDraftSelectedAccounts();
    let rules;
    try {
      rules = JSON.parse($("drawerRulesJson").value || "[]");
    } catch (error) {
      throw new Error("规则 JSON 格式错误");
    }
    if (!Array.isArray(rules) || !rules.length) throw new Error("至少需要一条规则");
    const objectLevel = $("drawerObjectLevel").value || "campaign";
    const sharedCopyConfig = copyConfigFromDrawer();
    const sharedCopyPayload = copyPayloadFromDrawer();
    const sharedDramaScope = dramaScopeFromDrawer();
    const sharedCandidateSelection = candidateSelectionFromDrawer();
    const sharedTopN = sharedCandidateSelection.mode === "top_n_per_account" ? sharedCandidateSelection.top_n : 0;
    const editableCopyRuleIndex = firstCopyRuleIndex(rules);
    rules = rules.map((rule, index) => {
      if (!rule || typeof rule !== "object") throw new Error(`第 ${index + 1} 条规则格式错误`);
      const action = String(rule.action || "").toLowerCase();
      if (!["pause", "copy"].includes(action)) throw new Error(`第 ${index + 1} 条规则 action 仅允许 pause 或 copy`);
      const item = Object.assign({}, rule, { action, level: objectLevel, enabled: rule.enabled !== false });
      if (action === "copy" && index === editableCopyRuleIndex) {
        const ruleCandidateSelection = rule.candidate_selection && typeof rule.candidate_selection === "object" ? rule.candidate_selection : {};
        const itemCandidateSelection = Object.assign({}, ruleCandidateSelection, sharedCandidateSelection);
        itemCandidateSelection.mode = sharedCandidateSelection.mode;
        const itemTopN = itemCandidateSelection.mode === "top_n_per_account" ? sharedTopN : 0;
        itemCandidateSelection.top_n = itemTopN;
        item.copy = mergeRuleCopy(rule.copy || rule.copy_config, sharedCopyPayload);
        item.drama_scope = Object.assign({}, rule.drama_scope || {}, sharedDramaScope);
        item.candidate_selection = itemCandidateSelection;
        item.top_n_per_account = itemTopN;
        delete item.copy_config;
      } else if (action !== "copy") {
        delete item.copy;
        delete item.copy_config;
        delete item.drama_scope;
        delete item.candidate_selection;
        delete item.top_n_per_account;
      }
      return item;
    });
    const accountIds = Array.from(draft.selectedAccountKeys).map(normalizeAccountId).filter(Boolean);
    if (!accountIds.length) throw new Error("请至少选择一个账号");
    const runMode = document.querySelector('input[name="drawerRunMode"]:checked')?.value || "observe";
    if (objectLevel === "ad" && runMode === "live") throw new Error("广告 Ad 第二阶段目前只允许保存配置，不能切换正式执行");
    const scheduleType = $("drawerScheduleType").value || "fixed_time";
    const schedule = {
      type: scheduleType,
      time: $("drawerExecuteTime").value || "",
      interval_minutes: Number($("drawerIntervalMinutes").value || 0),
      allowed_start: $("drawerAllowedStart").value || "",
      allowed_end: $("drawerExecuteBefore").value || "",
      timezone: "account",
    };
    if (scheduleType === "fixed_time" && !schedule.time) throw new Error("请选择每天执行时间");
    if (scheduleType === "interval" && schedule.interval_minutes < 5) throw new Error("间隔执行最小为 5 分钟");
    if (!schedule.allowed_start || !schedule.allowed_end) throw new Error("请设置允许执行的开始和截止时间");
    const limits = {
      rule_daily_limit: Number($("drawerRuleDailyLimit").value || 0),
      user_daily_limit: Number($("drawerUserDailyLimit").value || 0),
      source_cooldown_days: Number($("drawerSourceCooldown").value || 0),
    };
    const hasCopy = rules.some(rule => rule.action === "copy");
    if (hasCopy) {
      if (limits.rule_daily_limit < 1 || limits.user_daily_limit < 1) throw new Error("每日复制额度必须大于 0");
      if (sharedDramaScope.type === "recent_days" && sharedDramaScope.recent_days < 1) throw new Error("最近剧目天数必须大于 0");
      if (sharedDramaScope.type === "specified" && !sharedDramaScope.drama_ids.length) throw new Error("请填写指定剧 ID 或 series_code");
      rules.forEach((rule, index) => {
        if (rule.action !== "copy") return;
        const selection = rule.candidate_selection;
        if (selection != null && (typeof selection !== "object" || Array.isArray(selection))) {
          throw new Error(`第 ${index + 1} 条复制规则的 candidate_selection 必须是对象`);
        }
        const selectionMode = String(selection?.mode || "").toLowerCase();
        if (selectionMode && !["all", "top_n_per_account", "top_n", "topn", "top"].includes(selectionMode)) {
          throw new Error(`第 ${index + 1} 条复制规则的候选选择仅允许 all 或 top_n_per_account`);
        }
        const explicitTopN = rule.top_n_per_account ?? selection?.top_n_per_account ?? selection?.top_n;
        if (["top_n_per_account", "top_n", "topn", "top"].includes(selectionMode) && (!Number.isFinite(Number(explicitTopN)) || Number(explicitTopN) < 1)) {
          throw new Error(`第 ${index + 1} 条复制规则的每账号 Top N 必须大于 0`);
        }
      });
      if (sharedCopyConfig.budget_strategy === "actual_cpi_multiplier" && sharedCopyConfig.cpi_multiplier <= 0) throw new Error("实际 CPI 倍数必须大于 0");
      if (sharedCopyConfig.budget_strategy === "fixed_target_cpi_multiplier" && (sharedCopyConfig.fixed_target_cpi <= 0 || sharedCopyConfig.cpi_multiplier <= 0)) throw new Error("固定目标 CPI 和倍数必须大于 0");
      if (sharedCopyConfig.budget_strategy === "source_budget_ratio" && sharedCopyConfig.source_budget_ratio <= 0) throw new Error("来源预算比例必须大于 0");
      if (sharedCopyConfig.roas_percent < 0 || sharedCopyConfig.roas_percent > 100) throw new Error("ROAS 调整百分比必须在 0 到 100 之间");
    }
    const strategyCopy = hasCopy ? Object.assign({}, sharedCopyPayload, {
      allowed_after: schedule.allowed_start,
      allowed_before: schedule.allowed_end,
      schedule,
      interval_minutes: schedule.interval_minutes,
      candidate_selection: sharedCandidateSelection,
      top_n_per_account: sharedTopN,
      daily_rule_limit: limits.rule_daily_limit,
      daily_user_limit: limits.user_daily_limit,
      source_cooldown_days: limits.source_cooldown_days,
      drama_scope: sharedDramaScope,
    }) : {};
    const payload = {
      group_id: draft.id,
      name: $("drawerGroupName").value.trim(),
      account_ids: accountIds,
      object_level: objectLevel,
      run_mode: runMode,
      rules,
      default_window: { type: $("drawerWindowType").value || "since_start", hours: Number($("drawerWindowHours").value || 24) },
      enabled: false,
      strategy: {
        frontend_rule_group_id: draft.id,
        frontend_rule_group_name: $("drawerGroupName").value.trim(),
        description: $("drawerGroupDescription").value.trim(),
        selected_account_ids: accountIds,
        account_count: accountIds.length,
        object_level: objectLevel,
        run_mode: runMode,
        schedule,
        limits: hasCopy ? limits : {},
        drama_scope: hasCopy ? sharedDramaScope : {},
        candidate_selection: hasCopy ? sharedCandidateSelection : {},
        top_n_per_account: hasCopy ? sharedTopN : 0,
        copy: strategyCopy,
        execute_timezone: "account",
      },
    };
    if (draft.mode === "edit") {
      payload.migrate_from_group_ids = Array.from(new Set((draft.migrate_from_group_ids || [])
        .map(value => String(value || "").trim()).filter(value => value && value !== payload.group_id)));
    }
    if (runMode === "live" && draft.run_mode !== "live") {
      if (!draft.liveModeConfirmed) throw new Error("切换正式执行需要完成二次确认");
      payload.confirm = "ENABLE_LIVE_MODE";
      payload.live_mode_confirm = "ENABLE_LIVE_MODE";
    }
    return payload;
  }

  function updateRuleGroupSummary() {
    const box = $("drawerSummary");
    if (!box || !state.ruleGroupDraft) return;
    let summary;
    try {
      summary = readRuleGroupDraftFromDrawer();
    } catch (error) {
      box.innerHTML = `<div class="hint">${escapeHtml(error.message)}</div>`;
      return;
    }
    const actions = summary.rules.map(rule => rule.action === "copy" ? "复制" : "关闭");
    const hasCopy = summary.rules.some(rule => rule.action === "copy");
    const candidateSelection = summary.strategy.candidate_selection || {};
    const candidateSelectionLabel = !hasCopy
      ? "不适用"
      : (candidateSelection.mode === "all" ? "全部符合条件" : `每账号 Top ${candidateSelection.top_n}`);
    box.innerHTML = `<div class="summary-grid"><div><span>调控对象</span><strong>${escapeHtml(OBJECT_LEVEL_LABELS[summary.object_level])}</strong></div><div><span>账号</span><strong>${summary.account_ids.length} 个</strong></div><div><span>命中动作</span><strong>${escapeHtml(Array.from(new Set(actions)).join(" / "))}</strong></div><div><span>运行模式</span><strong>${escapeHtml(RUN_MODE_LABELS[summary.run_mode])}</strong></div><div><span>候选选择</span><strong>${escapeHtml(candidateSelectionLabel)}</strong></div></div>
      <div class="hint">保存请求仅包含账号、对象层级、运行模式、规则和策略；不提交产品或 owner。保存后默认 disabled。</div>`;
  }

  async function saveFrontendRuleGroup() {
    let payload;
    try {
      payload = readRuleGroupDraftFromDrawer();
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    if (!payload.name) return toast("请填写规则组名称", "error");
    try {
      await api("/api/ad-control/rule-groups", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast("规则组已保存，当前保持禁用");
      closeRuleGroupDrawer();
      await refreshRuleGroups();
    } catch (error) {
      toast(error.message || String(error), "error");
    }
  }

  function ruleGroupTargetIds(group) {
    const ids = Array.isArray(group?.target_ids) && group.target_ids.length
      ? group.target_ids.map(value => String(value || "").trim()).filter(Boolean)
      : (group.bindings || []).map(bindingId).filter(Boolean);
    return ids.length ? Array.from(new Set(ids)) : [group.id];
  }

  function firstPreviewErrorReason(results) {
    for (const result of results || []) {
      for (const error of result?.errors || []) {
        const reason = typeof error === "string" ? error : (error?.reason || error?.message || error?.code);
        if (reason) return String(reason);
      }
    }
    return "";
  }

  async function previewFrontendRuleGroup(group) {
    const results = await Promise.all(ruleGroupTargetIds(group).map(id => api(`/api/ad-control/rule-groups/${encodeURIComponent(id)}/preview-live`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: true }),
    })));
    const targets = results.reduce((sum, item) => sum + Number(item.execution_count ?? item.pause_count ?? item.copy_count ?? 0), 0);
    const errors = results.reduce((sum, item) => sum + Number(item.error_count || 0), 0);
    const firstReason = firstPreviewErrorReason(results);
    toast(`试算完成：拟执行 ${targets} 个，错误 ${errors} 个${firstReason ? `；首个原因 ${firstReason}` : ""}；未调用 Meta 写接口`);
    await refreshRuleGroups();
  }

  async function previewDraftRuleGroup() {
    const draft = state.ruleGroupDraft;
    if (!draft?.sourceGroup || draft.mode !== "edit") return toast("请先保存规则组，再执行独立试算", "error");
    await previewFrontendRuleGroup(draft.sourceGroup);
  }

  async function setFrontendRuleGroupEnabled(group, enabled) {
    const label = enabled ? "启用" : "禁用";
    let liveModeConfirmed = false;
    if (enabled) {
      if (group.object_level === "ad") {
        return toast("Ad 第二阶段目前仅允许保存配置，暂不能启用；runner 会返回 phase_not_enabled", "error");
      }
      const message = group.run_mode === "live"
        ? (group.has_copy_action
          ? "当前规则组包含 copy。复制落表尚未确认，正式 copy 会被后端 fail-closed；pause 仍可能正式执行。确认继续第一步？"
          : "当前规则组是正式执行模式，启用后 pause 会按计划调用 Meta 写接口。确认继续第一步？")
        : "当前规则组是观察模式，启用后只会扫描和记录拟执行结果。确认继续？";
      if (!confirm(message)) return;
      if (group.run_mode === "live" && prompt("二次确认：请输入 ENABLE_LIVE_MODE") !== "ENABLE_LIVE_MODE") {
        return toast("二次确认未通过，规则组保持禁用", "error");
      }
      liveModeConfirmed = group.run_mode === "live";
    }
    const results = await Promise.allSettled(ruleGroupTargetIds(group).map(id => api(`/api/ad-control/rule-groups/${encodeURIComponent(id)}/enabled`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign(
        { enabled },
        enabled && group.run_mode === "live" && liveModeConfirmed ? { live_mode_confirm: "ENABLE_LIVE_MODE" } : {},
      )),
    })));
    const failed = results.filter(item => item.status === "rejected");
    await refreshRuleGroups();
    if (failed.length) throw new Error(`${label}失败：${failed.map(item => item.reason?.message || String(item.reason)).join("；")}`);
    toast(`${label}完成`);
  }

  async function emergencyStopFrontendRuleGroup(group) {
    if (!confirm("确认急停当前规则组？只停止该规则组后续动作，不会主动恢复或删除广告。")) return;
    for (const id of ruleGroupTargetIds(group)) {
      await api("/api/ad-control/emergency-stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "rule_group", group_id: id }),
      });
    }
    toast("当前规则组已急停");
    await refreshRuleGroups();
  }

  async function deleteFrontendRuleGroup(group) {
    if (!confirm("确认删除当前规则组？历史执行日志不会删除。")) return;
    for (const id of ruleGroupTargetIds(group)) {
      await api(`/api/ad-control/rule-groups/${encodeURIComponent(id)}`, { method: "DELETE" });
    }
    toast("规则组已删除");
    await refreshRuleGroups();
  }

  function openRuleGroupLogs(group) {
    const id = ruleGroupTargetIds(group)[0];
    if (!id) return toast("规则组没有日志标识", "error");
    window.location.assign(`/ad-control-logs.html?binding_id=${encodeURIComponent(id)}`);
  }

  async function renderPools() {
    $("pageRoot").innerHTML = `${productFilter()}<section class="panel"><div class="panel-head"><h2>账户池</h2><div class="row"><button class="btn" id="newPoolBtn">新建账户池</button><button class="btn primary" id="savePoolBtn">保存账户池</button></div></div><div class="panel-body">
      <div class="grid two"><div class="field"><label>账户池名称</label><input id="poolName" placeholder="北美 +8 调控账户" /></div><div class="field"><label>账户池 ID</label><input id="poolId" readonly /></div></div>
      <div class="row"><button class="btn" id="selectAllBtn">全选账户</button><button class="btn" id="clearBtn">清空账户</button><span class="hint" id="accountHint"></span></div><div class="account-list" id="accountList"></div>
      <div class="manual-account-box" style="margin-top:12px;"><div class="field"><label>手动添加账号 ID</label><textarea id="poolManualAccounts" class="short-textarea" placeholder="支持粘贴 act_1146901540906487、1026707669580137；多个账号用换行、逗号或空格分隔"></textarea><span class="hint">手动账号会写入当前产品的账户池；适合账号未出现在接口列表时使用。</span></div><button class="btn" id="poolAddManualAccounts" type="button">加入账户池</button></div>
      </div></section><section class="panel"><div class="panel-head"><h2>账户池列表</h2></div><div class="panel-body"><div class="list" id="poolList"></div></div></section>`;
    await loadProducts(); await refreshPoolPage();
    $("productSelect").onchange = () => { resetPoolForm(); refreshPoolPage(); };
    $("newPoolBtn").onclick = resetPoolForm;
    $("savePoolBtn").onclick = savePool;
    $("selectAllBtn").onclick = () => document.querySelectorAll("#accountList input").forEach(input => input.checked = true);
    $("clearBtn").onclick = () => document.querySelectorAll("#accountList input").forEach(input => input.checked = false);
    $("poolAddManualAccounts").onclick = addPoolManualAccounts;
    $("accountList").onclick = event => {
      const remove = event.target.closest("[data-remove-pool-account]");
      if (!remove) return;
      event.preventDefault();
      renderAccounts(selectedAccounts().filter(id => id !== normalizeAccountId(remove.dataset.removePoolAccount)));
    };
  }
  async function refreshPoolPage() {
    state.accounts = [];
    await loadPools();
    renderPoolList();
    $("accountHint").textContent = `已显示 ${state.pools.length} 个已保存账户池，正在加载业务库账户...`;
    const selected = selectedAccounts();
    try {
      const data = await loadAccounts(selected);
      $("accountHint").textContent = data.warning || `已加载 ${state.accounts.length} 个账户`;
    } catch (error) {
      state.accounts = [];
      renderAccounts(selected);
      $("accountHint").textContent = `业务库账户加载失败：${error.message}；已保存账户池仍可查看和编辑`;
    }
  }
  async function savePool() {
    const name = $("poolName").value.trim();
    const accountIds = selectedAccounts();
    if (!name) return toast("请填写账户池名称", "error");
    if (!accountIds.length) return toast("请至少选择或手动添加一个账号", "error");
    try {
      await api("/api/ad-control/account-groups", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ group_id: $("poolId").value, product: product(), name, account_ids: accountIds }) });
      toast("账户池已保存"); resetPoolForm(); await refreshPoolPage();
    } catch (error) {
      toast(`账户池保存失败：${error.message}`, "error");
    }
  }
  function resetPoolForm() {
    $("poolId").value = "";
    $("poolName").value = "";
    if ($("poolManualAccounts")) $("poolManualAccounts").value = "";
    renderAccounts([]);
  }
  function addPoolManualAccounts() {
    const ids = Array.from(new Set(splitValues($("poolManualAccounts").value).map(normalizeAccountId).filter(Boolean)));
    if (!ids.length) return toast("请粘贴账号 ID", "error");
    const selected = new Set(selectedAccounts());
    ids.forEach(id => selected.add(id));
    renderAccounts(Array.from(selected));
    $("poolManualAccounts").value = "";
    toast(`已加入 ${ids.length} 个账号`);
  }
  function renderPoolList() {
    $("poolList").innerHTML = state.pools.length ? state.pools.map(item => {
      const accountIds = item.account_ids || [];
      const preview = accountIds.slice(0, 6).map(value => `act_${normalizeAccountId(value)}`).join("、");
      const more = accountIds.length > 6 ? ` 等 ${accountIds.length} 个` : "";
      return `<div class="item"><div><strong>${escapeHtml(item.name)}</strong><span class="hint">${escapeHtml(item.product || product())} / ${escapeHtml(item.group_id)} / ${accountIds.length} 个账户</span><span class="hint mono">${escapeHtml(preview)}${escapeHtml(more)}</span></div><div class="row"><button class="btn" data-edit-pool="${escapeHtml(item.group_id)}">查看/编辑</button><button class="btn danger" data-delete-pool="${escapeHtml(item.group_id)}">删除</button></div></div>`;
    }).join("") : `<div class="empty">当前产品暂无账户池</div>`;
    $("poolList").onclick = async event => {
      const edit = event.target.closest("[data-edit-pool]");
      const del = event.target.closest("[data-delete-pool]");
      if (edit) {
        const item = state.pools.find(pool => pool.group_id === edit.dataset.editPool);
        if (!item) return toast("账户池不存在，请刷新", "error");
        $("poolId").value = item.group_id; $("poolName").value = item.name; renderAccounts(item.account_ids);
      }
      if (del && confirm("确认删除账户池？")) {
        try {
          await api(`/api/ad-control/account-groups/${encodeURIComponent(del.dataset.deletePool)}`, { method: "DELETE" });
          resetPoolForm(); await refreshPoolPage();
        } catch (error) {
          toast(`账户池删除失败：${error.message}`, "error");
        }
      }
    };
  }

  async function renderBindings() {
    $("pageRoot").innerHTML = `<section class="panel"><div class="panel-head"><h2>跨区调控绑定向导</h2><button class="btn primary" id="batchBindingBtn">批量创建绑定</button></div><div class="panel-body">
      <div class="field"><label>投放产品（多选）</label><div class="check-list compact" id="bindingProductMulti"></div></div>
      <div class="grid"><div class="field"><label>绑定名前缀</label><input id="batchBindingPrefix" value="+8 跨区国家组关停" /></div><div class="field"><label>账户池名称包含</label><input id="batchPoolKeyword" value="北美 +8 调控账户" /></div><div class="field"><label>规则集名称包含</label><input id="batchRuleKeyword" value="+8 跨区国家组关停" /></div><div class="field"><label>国家组</label><input id="bindingCountryGroups" value="${escapeHtml(countryGroups().join(","))}" /></div></div>
      <div class="grid"><div class="field"><label>关闭时间</label><input id="bindingCloseTime" type="time" value="16:00" /></div><div class="field"><label>执行时区</label><input id="bindingExecuteTimezone" value="account" /></div><div class="field"><label>&nbsp;</label><label class="check-inline"><input id="bindingBlockSameDay" type="checkbox" checked /> 当天禁止重启</label></div><div class="field"><label>&nbsp;</label><label class="check-inline"><input id="bindingAllowNextDay" type="checkbox" checked /> 隔天允许程序重启</label></div></div>
      <div class="risk">批量创建只保存 disabled 绑定，不会自动执行。账户池和规则集按名称包含匹配，每个产品单独生成一条绑定。</div>
      <div class="list" id="batchBindingResult"></div>
      </div></section>${productFilter()}<section class="panel"><div class="panel-head"><h2>绑定关系</h2><div class="row"><button class="btn" id="newBindingBtn">新建</button><button class="btn primary" id="saveBindingBtn">保存绑定</button></div></div><div class="panel-body">
      <div class="grid"><div class="field"><label>绑定名称</label><input id="bindingName" placeholder="北美账户池 + 7小时规则" /></div><div class="field"><label>绑定 ID</label><input id="bindingId" readonly /></div><div class="field"><label>账户池</label><select id="poolSelect"></select></div><div class="field"><label>规则集</label><select id="ruleSetSelect"></select></div></div>
      <div class="grid"><div class="field"><label>关闭时间</label><input id="bindingStrategyCloseTime" type="time" /></div><div class="field"><label>执行时区</label><input id="bindingStrategyTimezone" placeholder="account / UTC+8" /></div><div class="field"><label>&nbsp;</label><label class="check-inline"><input id="bindingStrategyBlockSameDay" type="checkbox" /> 当天禁止重启</label></div><div class="field"><label>&nbsp;</label><label class="check-inline"><input id="bindingStrategyNextDay" type="checkbox" /> 隔天允许重启</label></div></div>
      <div class="risk">新绑定默认禁用。启用时会直接校验账户池及所有账号的 Token 权限，不再要求 Preview。</div></div></section><section class="panel"><div class="panel-head"><h2>绑定列表</h2></div><div class="panel-body"><div class="list" id="bindingList"></div></div></section>`;
    await loadProducts(); renderProductChecks("bindingProductMulti", state.products, state.products.slice(0, 2).map(item => item.product)); await refreshBindingPage();
    $("productSelect").onchange = refreshBindingPage;
    $("newBindingBtn").onclick = () => fillBinding(null);
    $("saveBindingBtn").onclick = saveBinding;
    $("batchBindingBtn").onclick = saveBatchBindings;
  }
  async function refreshBindingPage() {
    await Promise.all([loadPools(), loadRuleSets(), loadBindings()]);
    $("poolSelect").innerHTML = optionHtml(state.pools, "group_id", "name", "请选择账户池");
    $("ruleSetSelect").innerHTML = optionHtml(state.ruleSets, "rule_set_id", "name", "请选择规则集");
    fillBinding(null); renderBindingList();
  }
  function fillBinding(item) {
    $("bindingId").value = item ? item.binding_id : "";
    $("bindingName").value = item ? item.name : "";
    $("poolSelect").value = item ? item.account_group_id : "";
    $("ruleSetSelect").value = item ? item.rule_set_id : "";
    const strategy = (item && item.strategy) || {};
    $("bindingStrategyCloseTime").value = strategy.close_time || "";
    $("bindingStrategyTimezone").value = strategy.execute_timezone || "";
    $("bindingStrategyBlockSameDay").checked = !!strategy.block_same_day_reopen;
    $("bindingStrategyNextDay").checked = !!strategy.allow_next_day_reopen;
  }
  async function saveBinding() {
    await api("/api/ad-control/bindings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ group_id: $("bindingId").value, product: product(), name: $("bindingName").value.trim(), account_group_id: $("poolSelect").value, rule_set_id: $("ruleSetSelect").value, enabled: false, strategy: bindingStrategyFromForm() }) });
    toast("绑定已保存，默认禁用"); await refreshBindingPage();
  }
  function bindingStrategyFromForm() {
    return {
      close_time: $("bindingStrategyCloseTime").value || "",
      execute_timezone: $("bindingStrategyTimezone").value || "",
      block_same_day_reopen: $("bindingStrategyBlockSameDay").checked,
      allow_next_day_reopen: $("bindingStrategyNextDay").checked,
    };
  }
  function batchBindingStrategy() {
    return {
      close_time: $("bindingCloseTime").value || "",
      execute_timezone: $("bindingExecuteTimezone").value || "account",
      block_same_day_reopen: $("bindingBlockSameDay").checked,
      allow_next_day_reopen: $("bindingAllowNextDay").checked,
      country_groups: splitValues($("bindingCountryGroups").value),
    };
  }
  async function saveBatchBindings() {
    const products = selectedProducts("bindingProductMulti");
    if (!products.length) return toast("请选择投放产品", "error");
    const poolKeyword = ($("batchPoolKeyword").value || "").trim();
    const ruleKeyword = ($("batchRuleKeyword").value || "").trim();
    const prefix = ($("batchBindingPrefix").value || "+8 跨区国家组关停").trim();
    const results = [];
    for (const productValue of products) {
      const [poolsData, ruleData] = await Promise.all([
        api(`/api/ad-control/account-groups?product=${encodeURIComponent(productValue)}`),
        api(`/api/ad-control/rule-sets?product=${encodeURIComponent(productValue)}`),
      ]);
      const pool = (poolsData.items || []).find(item => !poolKeyword || String(item.name || "").includes(poolKeyword));
      const ruleSet = (ruleData.items || []).find(item => !ruleKeyword || String(item.name || "").includes(ruleKeyword));
      if (!pool || !ruleSet) {
        results.push({ product: productValue, status: "skipped", reason: !pool ? "未找到账户池" : "未找到规则集" });
        continue;
      }
      const payload = await api("/api/ad-control/bindings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product: productValue, name: `${prefix} / ${productValue}`, account_group_id: pool.group_id, rule_set_id: ruleSet.rule_set_id, enabled: false, strategy: batchBindingStrategy() }),
      });
      results.push({ product: productValue, status: "created", binding_id: payload.binding_id });
    }
    $("batchBindingResult").innerHTML = results.map(item => `<div class="item"><div><strong>${escapeHtml(item.product)}</strong><span class="hint">${escapeHtml(item.status)} ${escapeHtml(item.binding_id || item.reason || "")}</span></div></div>`).join("");
    toast(`批量绑定完成：${results.filter(item => item.status === "created").length}/${products.length}`);
    await refreshBindingPage();
  }
  function renderBindingList() {
    $("bindingList").innerHTML = state.bindings.length ? state.bindings.map(item => `<div class="item"><div><strong>${escapeHtml(item.name)}</strong><span class="hint">${escapeHtml(item.binding_id)} / 账户池 ${escapeHtml(item.account_group_id || "--")} / 规则集 ${escapeHtml(item.rule_set_name || item.rule_set_id || "--")} / ${item.enabled ? "已启用" : "已禁用"} / ${item.emergency_stopped ? "已急停" : "正常"} / ${escapeHtml(strategySummary(item.strategy))}</span></div><div class="row"><button class="btn" data-edit-binding="${escapeHtml(item.binding_id)}">编辑</button><button class="btn" data-toggle-binding="${escapeHtml(item.binding_id)}" data-enabled="${item.enabled ? "0" : "1"}">${item.enabled ? "禁用" : "启用"}</button><button class="btn danger" data-delete-binding="${escapeHtml(item.binding_id)}">删除</button></div></div>`).join("") : `<div class="empty">暂无绑定</div>`;
    $("bindingList").onclick = async event => {
      const edit = event.target.closest("[data-edit-binding]");
      const toggle = event.target.closest("[data-toggle-binding]");
      const del = event.target.closest("[data-delete-binding]");
      if (edit) fillBinding(state.bindings.find(item => item.binding_id === edit.dataset.editBinding));
      if (toggle) { await api(`/api/ad-control/bindings/${encodeURIComponent(toggle.dataset.toggleBinding)}/enabled`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: toggle.dataset.enabled === "1" }) }); await refreshBindingPage(); }
      if (del && confirm("确认删除绑定？")) { await api(`/api/ad-control/bindings/${encodeURIComponent(del.dataset.deleteBinding)}`, { method: "DELETE" }); await refreshBindingPage(); }
    };
  }

  async function renderRun() {
    $("pageRoot").innerHTML = `<section class="panel"><div class="panel-head"><h2>运行控制台已下线</h2></div><div class="panel-body"><div class="risk">规则组启停已统一收口到规则组管理页；启用前必须先“立即试算”生成有效 Preview，本页不再提供手动执行。</div><a class="btn primary" href="/ad-control-rules.html">返回规则组管理</a></div></section>`;
  }

  async function renderTokens() {
    $("pageRoot").innerHTML = `${productFilter()}<section class="panel"><div class="panel-head"><h2>默认Token来源</h2><div class="row"><button class="btn" id="validateBtn">校验当前产品</button><button class="btn" id="reloadBtn">刷新</button></div></div><div class="panel-body"><div class="risk">当前页面只读展示。规则执行会按目标产品读取 apps_setting.default_user，再从 ads_facebook_info 获取对应 Meta token。</div><div class="list" id="tokenList"></div><div class="list" id="tokenValidateResult"></div></div></section>`;
    await loadProducts(); await refreshTokenPage();
    $("productSelect").onchange = refreshTokenPage;
    $("reloadBtn").onclick = refreshTokenPage;
    $("validateBtn").onclick = validateToken;
  }
  async function refreshTokenPage() {
    await loadAccounts();
    const data = await api(`/api/ad-control/token-config?product=${encodeURIComponent(product())}`);
    $("tokenValidateResult").innerHTML = "";
    $("tokenList").innerHTML = (data.items || []).length ? data.items.map(item => `<div class="item"><div><strong>${escapeHtml(item.app_name || item.product || "产品默认")}</strong><span class="hint">default_user ${escapeHtml(item.user_id)} / apps_setting.id ${escapeHtml(item.app_id || "--")} / app_id ${escapeHtml(item.app_key || "--")} / 来源 ${escapeHtml(item.source || "--")}</span></div><span class="badge ${((item.validation || {}).ok) ? "ok" : "warn"}">${((item.validation || {}).ok) ? "可用于校验" : "未校验"}</span></div>`).join("") : `<div class="empty">当前产品未找到 apps_setting.default_user</div>`;
  }
  function tokenValidationAccounts() {
    return state.accounts.map(item => item.account_id).filter(Boolean);
  }
  async function validateToken() {
    try {
      const accounts = tokenValidationAccounts();
      const data = await api("/api/ad-control/token-config/validate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ product: product(), accounts }) });
      $("tokenValidateResult").innerHTML = `<div class="item"><div><strong>校验结果</strong><span class="hint">${escapeHtml(data.app_name || product())} / default_user ${escapeHtml(data.user_id || "--")} / ${escapeHtml(data.ok_count || 0)}/${escapeHtml(data.checked_count || 0)} 个账户可访问</span></div><span class="badge ${data.ok ? "ok" : "warn"}">${data.ok ? "通过" : "部分失败"}</span></div>`;
      toast(`校验完成：${data.ok_count}/${data.checked_count} 通过`);
    } catch (error) {
      toast(error.message || String(error), "error");
    }
  }

  async function renderLogs() {
    $("pageRoot").innerHTML = `${productFilter(`<div class="field"><label>规则组</label><select id="bindingFilter"><option value="">全部规则组</option></select></div>`)}<section class="panel"><div class="panel-head"><div><h2>执行日志</h2><span class="hint" id="logStorageHint">正在读取 ads_ai 调控日志...</span></div><button class="btn" id="loadLogsBtn">查询</button></div><div class="panel-body"><div class="grid"><div class="field"><label>展示方式</label><select id="viewFilter"><option value="daily">按业务日汇总</option><option value="raw">原始批次</option></select></div><div class="field"><label>动作</label><select id="actionFilter"><option value="">全部执行动作</option><option value="pause">关闭 pause</option><option value="copy">复制 copy</option><option value="mixed">混合 mixed</option><option value="reopen">重启 reopen</option></select></div><div class="field"><label>开始日期</label><input id="dateFrom" type="date" /></div><div class="field"><label>结束日期</label><input id="dateTo" type="date" /></div><div class="field"><label>显示数量</label><input id="limitInput" type="number" min="1" max="200" value="50" /></div></div><div class="list" id="actionList"></div></div></section>`;
    const params = new URLSearchParams(window.location.search);
    await loadProducts({ includeAll: true });
    if (params.get("product") && $("productSelect")) $("productSelect").value = params.get("product");
    await refreshLogBindings();
    if (params.get("binding_id")) $("bindingFilter").value = params.get("binding_id");
    await loadLogs();
    $("productSelect").onchange = async () => { await refreshLogBindings(); await loadLogs(); };
    $("viewFilter").onchange = loadLogs;
    $("loadLogsBtn").onclick = loadLogs;
  }
  async function refreshLogBindings() {
    await loadBindings();
    $("bindingFilter").innerHTML = optionHtml(state.bindings, "binding_id", "name", "全部规则组");
    const requested = new URLSearchParams(window.location.search).get("binding_id");
    if (requested && state.bindings.some(item => bindingId(item) === requested)) $("bindingFilter").value = requested;
  }
  async function loadLogs() {
    const sequence = ++state.logLoadSequence;
    const button = $("loadLogsBtn");
    const storageHint = $("logStorageHint");
    if (button) button.disabled = true;
    if (storageHint) storageHint.textContent = "正在读取 ads_ai 调控日志...";
    $("actionList").innerHTML = `<div class="empty">日志加载中...</div>`;
    const query = { view: "daily", product: product(), binding_id: $("bindingFilter").value || "", action: $("actionFilter").value || "", date_from: $("dateFrom").value || "", date_to: $("dateTo").value || "", limit: $("limitInput").value || "50", include_targets: "false" };
    query.view = $("viewFilter").value || query.view;
    const qs = new URLSearchParams(query);
    try {
      const data = await api(`/api/ad-control/actions?${qs.toString()}`);
      if (sequence !== state.logLoadSequence) return;
      if (storageHint) {
        storageHint.textContent = data.storage === "ads_ai"
          ? "日志来源：ads_ai.ad_control_action_log"
          : "日志来源：本地 SQLite 回退（ads_ai 暂不可用）";
        const responseView = data.view || query.view;
        if (responseView === "daily") {
          storageHint.textContent += ` / 已汇总 ${Number(data.group_count || (data.items || []).length)} 个业务日规则组，共读取 ${Number(data.raw_action_count || 0)} 个批次`;
          if (data.source_truncated) storageHint.textContent += ` / 读取达到1000批上限，已丢弃 ${Number(data.discarded_group_count || 0)} 个边界日不完整分组，请缩小日期范围`;
          else if (data.has_more_groups) storageHint.textContent += ` / 当前仅展示最近 ${Number((data.items || []).length)} 个完整日组`;
        } else if (data.has_more) {
          storageHint.textContent += ` / 当前仅展示最近 ${Number((data.items || []).length)} 条原始批次`;
        }
        storageHint.title = data.storage_error || "";
      }
      renderActionList(data.items || [], $("actionList"), data.view || query.view);
      bindLogLazyDetails($("actionList"));
    } catch (error) {
      if (sequence !== state.logLoadSequence) return;
      if (storageHint) storageHint.textContent = "执行日志加载失败";
      $("actionList").innerHTML = `<div class="empty">${escapeHtml(error.message || String(error))}</div>`;
      toast(error.message || String(error), "error");
    } finally {
      if (sequence === state.logLoadSequence && button) button.disabled = false;
    }
  }
  function logStatusBadge(audit) {
    const status = (audit || {}).status || {};
    return `<span class="badge ${escapeHtml(status.class || "warn")}">${escapeHtml(status.label || "--")}</span>`;
  }
  function logStatusValue(status) {
    status = status || {};
    return `<span class="badge ${escapeHtml(status.class || "warn")}">${escapeHtml(status.label || "--")}</span>`;
  }
  function logCountPill(label, value, type = "") {
    return `<span class="log-count ${type}"><b>${escapeHtml(value)}</b>${escapeHtml(label)}</span>`;
  }
  function logValue(value) {
    if (value === null || value === undefined || value === "") return "--";
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString() : "--";
  }
  function firstLogValue(...values) {
    return values.find(value => value !== null && value !== undefined && value !== "");
  }
  function logLocalTime(value) {
    const text = String(value || "").trim();
    if (!text) return "--";
    const normalized = /^20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(text) ? `${text.replace(" ", "T")}Z` : text;
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return text;
    try {
      return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date).replace(/\//g, "-");
    } catch (error) {
      return text;
    }
  }
  function logFlow(item, audit) {
    const criteria = item.criteria || {};
    const summary = criteria.execution_summary || {};
    const flow = audit.flow || {};
    const legacyMetric = (field, flowValue, criteriaKey) => {
      const direct = item[field];
      const hasCriteria = Object.prototype.hasOwnProperty.call(criteria, criteriaKey);
      if (Number(item.log_version || 1) < 2 && Number(direct || 0) === 0 && !hasCriteria) return null;
      return firstLogValue(direct, flowValue, criteria[criteriaKey]);
    };
    const scanned = legacyMetric("scanned_count", flow.scanned, "scan_count");
    const candidate = legacyMetric("candidate_count", flow.candidate, "candidate_count");
    const matched = firstLogValue(item.matched_count, flow.matched, criteria.execution_target_count);
    const planned = firstLogValue(item.batch_planned_count, flow.batch_planned, criteria.execution_batch_count, item.requested_count);
    const remaining = firstLogValue(item.remaining_count, flow.remaining, summary.remaining_count);
    const retryable = firstLogValue(item.retryable_error_count, flow.retryable, summary.retryable_error_count);
    const stages = [
      ["本轮扫描", scanned],
      ["白名单候选", candidate],
      ["规则命中", matched],
      ["本批计划", planned],
      ["待后续处理", remaining],
    ];
    return `<div class="log-flow">${stages.map(([label, value], index) => `${index ? '<span class="log-flow-arrow">→</span>' : ""}<div class="log-stage ${label === "待后续处理" && Number(value || 0) > 0 ? "pending" : ""}"><span>${escapeHtml(label)}</span><b>${escapeHtml(logValue(value))}</b></div>`).join("")}</div>${Number(retryable || 0) > 0 ? `<div class="log-retry-note">其中 ${escapeHtml(logValue(retryable))} 个为可重试失败，后续批次会重新处理。</div>` : ""}`;
  }
  function logReasonList(items, emptyText) {
    const list = items || [];
    if (!list.length) return `<span class="hint">${escapeHtml(emptyText || "无")}</span>`;
    return `<div class="log-reasons">${list.map(item => `<span title="${escapeHtml(item.reason || "")}">${escapeHtml(logReasonLabel(item.reason))}<b>${escapeHtml(item.count)}</b></span>`).join("")}</div>`;
  }
  function logReasonLabel(value) {
    const text = String(value || "").trim();
    if (!text) return "--";
    try {
      const payload = JSON.parse(text);
      const error = payload && typeof payload === "object" ? (payload.error || payload) : {};
      const title = error.error_user_title || error.message || error.type || "Graph API 错误";
      const codes = [error.code != null ? `code ${error.code}` : "", error.error_subcode != null ? `subcode ${error.error_subcode}` : ""].filter(Boolean).join(" / ");
      return `${title}${codes ? `（${codes}）` : ""}`;
    } catch (error) {
      return text.length > 96 ? `${text.slice(0, 93)}...` : text;
    }
  }
  function logDailyFlow(item) {
    const stages = [
      ["首轮扫描", item.scanned_count],
      ["首轮规则命中", item.matched_count],
      ["执行批次", item.execution_batch_count],
      ["执行尝试", item.attempt_count],
      ["最终待处理", item.remaining_count],
    ];
    return `<div class="log-flow">${stages.map(([label, value], index) => `${index ? '<span class="log-flow-arrow">→</span>' : ""}<div class="log-stage ${label === "最终待处理" && Number(value || 0) > 0 ? "pending" : ""}"><span>${escapeHtml(label)}</span><b>${escapeHtml(logValue(value))}</b></div>`).join("")}</div>`;
  }
  function logSampleTable(samples) {
    const rows = samples || [];
    if (!rows.length) return `<div class="empty compact-empty">暂无目标明细</div>`;
    return `<div class="table-wrap compact-table log-sample-table"><table><thead><tr><th>结果</th><th>账号</th><th>Campaign</th><th>剧ID / 资源ID</th><th>语言 / 国家</th><th>原因 / 备注</th></tr></thead><tbody>${rows.map(row => {
      const notes = [row.reason || ""].concat(row.warnings || []).filter(Boolean);
      const campaignTitle = [row.campaign_name || "", row.campaign_id || ""].filter(Boolean).join(" / ");
      const campaignText = row.campaign_name ? `<div>${escapeHtml(row.campaign_name)}</div><div class="mono hint">${escapeHtml(row.campaign_short || row.campaign_id || "--")}</div>` : `<span class="mono" title="${escapeHtml(row.campaign_id || "")}">${escapeHtml(row.campaign_short || row.campaign_id || "--")}</span>`;
      const resourceText = row.resource_display || row.series_code || row.resource_id || row.source_id || "--";
      const resourceHint = row.resource_name || row.content_id || (row.original_source_id ? `source ${row.original_source_id}` : "");
      return `<tr><td>${escapeHtml(row.status_label || row.status || "--")}</td><td class="mono">${escapeHtml(row.account_id || "--")}</td><td title="${escapeHtml(campaignTitle)}">${campaignText}</td><td><div class="mono">${escapeHtml(resourceText)}</div>${resourceHint ? `<div class="hint">${escapeHtml(resourceHint)}</div>` : ""}</td><td>${escapeHtml(row.language || "--")}<div class="hint">${escapeHtml(row.country || "")}</div></td><td>${escapeHtml(notes.join("；") || "--")}</td></tr>`;
    }).join("")}</tbody></table></div>`;
  }
  async function loadLogTargets(details) {
    if (!details || details.dataset.targetsLoaded) return;
    const actionId = details.dataset.actionId || "";
    if (!actionId) return;
    const targetBody = details.querySelector("[data-target-body]");
    const targetSummary = details.querySelector("[data-target-summary]");
    const rawBody = details.querySelector("[data-raw-body]");
    const rawSummary = details.querySelector("[data-raw-summary]");
    details.dataset.targetsLoaded = "loading";
    if (targetBody) targetBody.innerHTML = `<div class="empty compact-empty">目标明细加载中...</div>`;
    try {
      const data = await api(`/api/ad-control/actions/${encodeURIComponent(actionId)}/targets`);
      const samples = data.samples || [];
      const results = data.results || [];
      const total = data.raw_result_count ?? results.length ?? samples.length;
      if (targetSummary) targetSummary.textContent = `目标明细（已展示 ${samples.length} / 共 ${total} 条）`;
      if (targetBody) targetBody.innerHTML = logSampleTable(samples);
      if (rawSummary) rawSummary.textContent = `原始结果 JSON（${results.length} 条）`;
      if (rawBody) rawBody.textContent = JSON.stringify(results, null, 2);
      details.dataset.targetsLoaded = "1";
    } catch (error) {
      details.dataset.targetsLoaded = "";
      const message = escapeHtml(error.message || String(error));
      if (targetBody) targetBody.innerHTML = `<div class="empty compact-empty">${message}</div>`;
      if (rawBody) rawBody.textContent = error.message || String(error);
    }
  }
  function bindLogLazyDetails(node) {
    if (!node || node.dataset.lazyBound === "1") return;
    node.dataset.lazyBound = "1";
    node.addEventListener("toggle", event => {
      const details = event.target.closest("details[data-lazy-targets]");
      if (!details || !details.open) return;
      loadLogTargets(details);
    }, true);
  }
  function logRawCount(item, audit) {
    return Math.max(
      Number((audit || {}).raw_result_count || 0),
      Number(item.requested_count || 0),
      Number(item.success_count || 0) + Number(item.skipped_count || 0) + Number(item.error_count || 0),
    );
  }
  function renderLazyLogDetails(actionId, rawCount, label) {
    return `<details class="log-details" data-lazy-targets="combined" data-action-id="${escapeHtml(actionId || "")}">
      <summary data-target-summary>${escapeHtml(label || "目标与原始结果")}（点击加载，共 ${escapeHtml(rawCount)} 条）</summary>
      <div data-target-body><div class="empty compact-empty">展开后加载目标明细</div></div>
      <div class="log-raw-head" data-raw-summary>原始结果 JSON（展开后加载）</div>
      <pre class="mono raw-json" data-raw-body>展开后加载原始结果 JSON</pre>
    </details>`;
  }
  function renderBatchRecord(batch, index) {
    const rawCount = logRawCount(batch, {});
    const batchKind = batch.verification_only ? "零目标完成复核" : `执行批次 ${index + 1}`;
    const batchStatus = Object.assign({}, batch.status || {}, { label: `当时状态：${(batch.status || {}).label || "--"}` });
    return `<div class="log-batch">
      <div class="log-batch-head"><div><strong>${escapeHtml(batchKind)}</strong><span class="hint">${escapeHtml(logLocalTime(batch.created_at))}</span></div>${logStatusValue(batchStatus)}</div>
      <div class="log-meta"><span>Action：<b class="mono">${escapeHtml(batch.action_id || "--")}</b></span><span>Preview：<b class="mono">${escapeHtml(batch.preview_id || "--")}</b></span></div>
      <div class="log-counts">
        ${logCountPill("计划", batch.requested_count || 0)}
        ${logCountPill("成功", batch.success_count || 0, "ok")}
        ${logCountPill("跳过", batch.skipped_count || 0, "warn")}
        ${logCountPill("失败", batch.error_count || 0, "danger")}
        ${logCountPill("剩余", batch.remaining_count || 0, Number(batch.remaining_count || 0) > 0 ? "warn" : "")}
      </div>
      ${(batch.reason_summary || []).length ? `<div class="log-section"><span class="log-label">本批原因</span>${logReasonList(batch.reason_summary, "无")}</div>` : ""}
      ${renderLazyLogDetails(batch.action_id, rawCount, "本批目标与原始结果")}
    </div>`;
  }
  function renderDailyLogCard(item) {
    const audit = item.audit || {};
    const counts = audit.counts || {};
    const strategy = (item.criteria || {}).strategy || {};
    const storage = audit.log_store || item.log_store || "sqlite_fallback";
    const batches = item.batches || [];
    return `<div class="log-card daily-log-card" data-group-id="${escapeHtml(item.group_id || "")}">
      <div class="log-card-head">
        <div class="log-title">
          <strong>${escapeHtml(audit.rule_group_name || item.binding_id || item.rule_identity || "--")}</strong>
          <span class="hint">业务日 ${escapeHtml(item.business_date || "--")} / ${escapeHtml(item.product || "--")} / ${escapeHtml(audit.action_label || item.action || "--")} / ${escapeHtml(audit.mode_label || (item.dry_run ? "Dry-run" : "正式执行"))}</span>
        </div>
        <div class="log-status">${logStatusBadge(audit)}</div>
      </div>
      <div class="log-meta">
        <span>规则组：<b class="mono">${escapeHtml(audit.rule_group_id || item.binding_id || item.rule_identity || "--")}</b></span>
        <span>批次记录：<b>${escapeHtml(item.batch_count || batches.length)}</b></span>
        <span>执行批次：<b>${escapeHtml(item.execution_batch_count || 0)}</b></span>
        <span>完成复核：<b>${escapeHtml(item.verification_batch_count || 0)}</b></span>
        <span>最后批次时间：<b>${escapeHtml(logLocalTime(item.last_created_at || item.created_at))}</b></span>
      </div>
      ${logDailyFlow(item)}
      <div class="log-channel-grid">
        <div class="log-channel"><span>执行尝试（含重试）</span><div class="log-counts">
          ${logCountPill("成功", counts.success ?? item.success_count ?? 0, "ok")}
          ${logCountPill("跳过", counts.skipped ?? item.skipped_count ?? 0, "warn")}
          ${logCountPill("失败", counts.error ?? item.error_count ?? 0, "danger")}
        </div><div class="log-attempt-note">同一目标被续跑或重试时会重复计数，不等于唯一目标数。</div></div>
        <div class="log-channel"><span>调控日志存储</span><strong class="log-store ${storage === "ads_ai" ? "ok" : "warn"}">${storage === "ads_ai" ? "已写入 ads_ai" : storage === "mixed" ? "ads_ai / SQLite 混合" : "SQLite 回退"}</strong></div>
      </div>
      <div class="log-section"><span class="log-label">原因（按尝试计数）</span>${logReasonList(audit.reason_summary || item.reason_summary, "无失败或跳过原因")}</div>
      ${item.status_inferred ? `<div class="log-status-note">历史日志缺少调度事件和续跑原因，主状态根据最终批次与剩余数保守推断。</div>` : ""}
      ${(audit.warning_summary || []).length ? `<div class="log-section"><span class="log-label">执行备注</span>${logReasonList(audit.warning_summary, "无备注")}</div>` : ""}
      ${strategy && Object.keys(strategy).length ? `<div class="log-section"><span class="log-label">最后批次策略</span><span class="hint">${escapeHtml(strategySummary(strategy))}</span></div>` : ""}
      <details class="log-batch-list"><summary>批次记录（${escapeHtml(batches.length)}）</summary><div class="log-batches">${batches.map(renderBatchRecord).join("") || '<div class="empty compact-empty">暂无批次记录</div>'}</div></details>
    </div>`;
  }
  function renderRawLogCard(item) {
      const audit = item.audit || {};
      const counts = audit.counts || {};
      const strategy = (item.criteria || {}).strategy || {};
      const rawCount = logRawCount(item, audit);
      const storage = audit.log_store || item.log_store || "sqlite_fallback";
      const eventKey = item.event_key || (item.criteria || {}).runner_event_key || "";
      return `<div class="log-card" data-action-id="${escapeHtml(item.action_id || "")}">
        <div class="log-card-head">
          <div class="log-title">
            <strong>${escapeHtml(audit.rule_group_name || item.binding_id || "--")}</strong>
            <span class="hint">${escapeHtml(audit.created_at_local || item.created_at || "--")} / ${escapeHtml(item.product || "--")} / ${escapeHtml(audit.action_label || item.action || "--")} / ${escapeHtml(audit.mode_label || (item.dry_run ? "Dry-run" : "正式执行"))}</span>
          </div>
          <div class="log-status">${logStatusBadge(audit)}</div>
        </div>
        <div class="log-meta">
          <span>规则组：<b class="mono">${escapeHtml(audit.rule_group_id || item.binding_id || "--")}</b></span>
          <span>Action：<b class="mono">${escapeHtml(item.action_id || "--")}</b></span>
          <span>Preview：<b class="mono">${escapeHtml(item.preview_id || "--")}</b></span>
          ${eventKey ? `<span>调度事件：<b class="mono">${escapeHtml(eventKey)}</b></span>` : ""}
        </div>
        ${logFlow(item, audit)}
        <div class="log-channel-grid">
          <div class="log-channel"><span>Meta 执行结果</span><div class="log-counts">
            ${logCountPill("成功", counts.success ?? item.success_count ?? 0, "ok")}
            ${logCountPill("跳过", counts.skipped ?? item.skipped_count ?? 0, "warn")}
            ${logCountPill("失败", counts.error ?? item.error_count ?? 0, "danger")}
          </div></div>
          <div class="log-channel"><span>调控日志存储</span><strong class="log-store ${storage === "ads_ai" ? "ok" : "warn"}">${storage === "ads_ai" ? "已写入 ads_ai" : "SQLite 回退"}</strong></div>
        </div>
        <div class="log-counts">
          ${logCountPill("本批计划", counts.requested ?? item.requested_count ?? 0)}
        </div>
        <div class="log-section">
          <span class="log-label">失败/跳过原因</span>
          ${logReasonList(audit.reason_summary, "无失败或跳过原因")}
        </div>
        ${(audit.warning_summary || []).length ? `<div class="log-section"><span class="log-label">执行备注</span>${logReasonList(audit.warning_summary, "无备注")}</div>` : ""}
        ${strategy && Object.keys(strategy).length ? `<div class="log-section"><span class="log-label">策略</span><span class="hint">${escapeHtml(strategySummary(strategy))}</span></div>` : ""}
        ${renderLazyLogDetails(item.action_id, rawCount, "目标与原始结果")}
      </div>`;
  }
  function renderActionList(items, node, view) {
    node.innerHTML = items.length ? items.map(item => (view === "daily" || item.is_daily_group) ? renderDailyLogCard(item) : renderRawLogCard(item)).join("") : `<div class="empty">暂无执行日志</div>`;
  }

  async function init() {
    const meta = TITLES[PAGE] || TITLES.overview;
    document.title = `${meta[0]} - AI自动后台`;
    $("pageTitle").textContent = meta[0];
    $("pageSubtitle").textContent = meta[1];
    $("topActions").innerHTML = pageHeaderActions();
    renderWarmQuickNav();
    $("refreshBtn").onclick = () => location.reload();
    $("authBtn").onclick = () => {
      requireSharedUi();
      return window.UiTopbar.handleAuthAction({ auth: state.auth || {}, api, afterLogout: () => window.location.assign("/") });
    };
    if (!(await loadAuth())) return;
    const renderers = { overview: renderOverview, rules: renderRules, pools: renderPools, bindings: renderBindings, run: renderRun, tokens: renderTokens, logs: renderLogs };
    await (renderers[PAGE] || renderOverview)();
  }

  document.addEventListener("DOMContentLoaded", () => init().catch(error => toast(error.message || String(error), "error")));
})();
