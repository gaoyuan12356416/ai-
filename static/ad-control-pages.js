(function () {
  const PAGE = document.body.dataset.page || "overview";
  const TITLES = {
    overview: ["AI自动规则调控", "查看调控中心状态、风险提示和常用入口。", "adControl"],
    rules: ["规则组管理", "用规则组统一管理产品、账号、规则阈值、绑定策略和执行入口。", "adControlRules"],
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
    ruleGroupAccounts: {},
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
          products: [],
          bindings: [],
          country_groups: Array.isArray(strategy.country_groups) ? strategy.country_groups : [],
          close_time: strategy.close_time || "",
          execute_timezone: strategy.execute_timezone || "",
          block_same_day_reopen: !!strategy.block_same_day_reopen,
          allow_next_day_reopen: !!strategy.allow_next_day_reopen,
          enabled: false,
          enabled_count: 0,
          partial_enabled: false,
          emergency_stopped: false,
          rule_count: 0,
          account_count: 0,
          updated_at: binding.updated_at || "",
        });
      }
      const group = map.get(id);
      group.bindings.push(binding);
      if (binding.product && !group.products.includes(binding.product)) group.products.push(binding.product);
      (strategy.selected_products || []).forEach(value => {
        if (ALLOWED_PRODUCTS.includes(value) && !group.products.includes(value)) group.products.push(value);
      });
      if (!group.description && strategy.description) group.description = strategy.description;
      if (!group.close_time && strategy.close_time) group.close_time = strategy.close_time;
      if (!group.execute_timezone && strategy.execute_timezone) group.execute_timezone = strategy.execute_timezone;
      group.block_same_day_reopen = group.block_same_day_reopen || !!strategy.block_same_day_reopen;
      group.allow_next_day_reopen = group.allow_next_day_reopen || !!strategy.allow_next_day_reopen;
      if (binding.enabled) group.enabled_count += 1;
      group.emergency_stopped = group.emergency_stopped || !!binding.emergency_stopped;
      group.rule_count += Array.isArray(binding.rules) ? binding.rules.length : 0;
      group.account_count += Number(strategy.account_count || (Array.isArray(strategy.selected_account_ids) ? strategy.selected_account_ids.length : 0) || (Array.isArray(binding.account_ids) ? binding.account_ids.length : 0) || 0);
      if (binding.updated_at && (!group.updated_at || binding.updated_at > group.updated_at)) group.updated_at = binding.updated_at;
      if ((!group.country_groups || !group.country_groups.length) && Array.isArray(strategy.country_groups)) group.country_groups = strategy.country_groups;
    });
    return Array.from(map.values()).map(group => {
      const total = group.bindings.length;
      group.enabled = total > 0 && group.enabled_count === total;
      group.partial_enabled = group.enabled_count > 0 && group.enabled_count < total;
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
  async function loadProducts() {
    state.products = productOptions();
    const select = $("productSelect");
    if (select) {
      const previous = select.value;
      select.innerHTML = state.products.map(item => `<option value="${escapeHtml(item.product)}">${escapeHtml(productLabel(item))}</option>`).join("");
      if (previous && ALLOWED_PRODUCTS.includes(previous)) select.value = previous;
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
          <div><h2>规则组管理</h2><span class="hint">一个前端规则组会按产品拆成底层规则集、账户池和绑定关系，新建后默认 disabled。</span></div>
          <div class="row"><button class="btn" id="reloadRuleGroupsBtn" type="button">刷新</button><button class="btn primary" id="newRuleGroupBtn" type="button">新建规则组</button></div>
        </div>
        <div class="panel-body">
          <div class="rule-toolbar">
            <div class="field"><label>搜索</label><input id="ruleGroupSearch" placeholder="规则组名称 / ID / 国家组" /></div>
            <div class="field"><label>产品筛选</label><select id="ruleGroupProductFilter"><option value="">全部三个产品</option>${ALLOWED_PRODUCTS.map(value => `<option value="${value}">${PRODUCT_LABELS[value]}</option>`).join("")}</select></div>
            <div class="field"><label>状态筛选</label><select id="ruleGroupStatusFilter"><option value="">全部</option><option value="enabled">已启用</option><option value="partial">部分启用</option><option value="disabled">已禁用</option><option value="stopped">已急停</option></select></div>
            <div class="field"><label>&nbsp;</label><button class="btn" id="clearRuleGroupFilterBtn" type="button">清空筛选</button></div>
          </div>
          <div class="risk">本页启停只修改后台规则状态；启用时会校验当前账户池及所有账号的 Token 权限，不会立即修改广告状态。</div>
          <div class="table-wrap compact-table"><table><thead><tr><th>规则组</th><th>产品范围</th><th>账号范围</th><th>规则</th><th>策略</th><th>状态</th><th>操作</th></tr></thead><tbody id="ruleGroupRows"></tbody></table></div>
        </div>
      </section>
      <div class="drawer-overlay hidden" id="ruleGroupDrawer">
        <div class="drawer-panel" role="dialog" aria-modal="true" aria-labelledby="drawerTitle">
          <div class="drawer-head"><div><h2 id="drawerTitle">新建规则组</h2><span class="hint" id="drawerSubTitle"></span></div><button class="btn" id="closeRuleGroupDrawerBtn" type="button">关闭</button></div>
          <div class="drawer-body" id="ruleGroupDrawerBody"></div>
          <div class="drawer-foot">
            <span class="hint" id="drawerSaveHint">保存后底层按产品拆分，且默认 disabled。</span>
            <div class="row"><button class="btn primary" id="saveRuleGroupBtn" type="button">保存规则组</button></div>
          </div>
        </div>
      </div>`;
    $("newRuleGroupBtn").onclick = () => openRuleGroupDrawer(null, false);
    $("reloadRuleGroupsBtn").onclick = refreshRuleGroups;
    $("ruleGroupSearch").oninput = renderRuleGroupList;
    $("ruleGroupProductFilter").onchange = renderRuleGroupList;
    $("ruleGroupStatusFilter").onchange = renderRuleGroupList;
    $("clearRuleGroupFilterBtn").onclick = () => {
      $("ruleGroupSearch").value = "";
      $("ruleGroupProductFilter").value = "";
      $("ruleGroupStatusFilter").value = "";
      renderRuleGroupList();
    };
    $("closeRuleGroupDrawerBtn").onclick = closeRuleGroupDrawer;
    $("saveRuleGroupBtn").onclick = saveFrontendRuleGroup;
    await refreshRuleGroups();
  }
  async function refreshRuleGroups() {
    const results = await Promise.all(ALLOWED_PRODUCTS.map(value => api(`/api/ad-control/bindings?product=${encodeURIComponent(value)}`).catch(() => ({ items: [] }))));
    state.bindings = results.flatMap(result => result.items || []);
    state.frontendRuleGroups = aggregateRuleGroups(state.bindings);
    renderRuleGroupList();
  }
  function filteredRuleGroups() {
    const query = ($("ruleGroupSearch")?.value || "").trim().toLowerCase();
    const productFilterValue = $("ruleGroupProductFilter")?.value || "";
    const status = $("ruleGroupStatusFilter")?.value || "";
    return state.frontendRuleGroups.filter(group => {
      const haystack = `${group.id} ${group.name} ${(group.country_groups || []).join(" ")} ${(group.products || []).join(" ")}`.toLowerCase();
      if (query && !haystack.includes(query)) return false;
      if (productFilterValue && !(group.products || []).includes(productFilterValue)) return false;
      if (status === "enabled" && !group.enabled) return false;
      if (status === "disabled" && (group.enabled || group.partial_enabled || group.emergency_stopped)) return false;
      if (status === "partial" && !group.partial_enabled) return false;
      if (status === "stopped" && !group.emergency_stopped) return false;
      return true;
    });
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
            ? `<span class="badge warn">部分启用 ${group.enabled_count}/${group.bindings.length}</span>`
            : `<span class="badge warn">已禁用</span>`));
      const toggleLabel = group.enabled ? "禁用" : (group.partial_enabled ? "继续启用" : "启用");
      return `<tr>
        <td><strong>${escapeHtml(group.name)}</strong><div class="mono">${escapeHtml(group.id)}</div><div class="hint">${escapeHtml(group.description || "无说明")}</div></td>
        <td><div class="chip-row">${productBadgeList(group.products)}</div></td>
        <td><strong>${group.account_count || "--"}</strong><div class="hint">${group.bindings.length} 个底层绑定</div></td>
        <td>${group.rule_count || "--"} 条<div class="hint">${escapeHtml(countryGroupLabel(group.country_groups || []))}</div></td>
        <td>${escapeHtml(strategySummary(group))}</td>
        <td>${stateLabel}</td>
        <td><div class="row action-row">
          <button class="btn" data-rule-action="edit" data-group="${escapeHtml(group.id)}" type="button">编辑</button>
          <button class="btn" data-rule-action="copy" data-group="${escapeHtml(group.id)}" type="button">复制</button>
          <button class="btn" data-rule-action="logs" data-group="${escapeHtml(group.id)}" type="button">日志</button>
          <button class="btn" data-rule-action="toggle" data-enabled="${group.enabled ? "0" : "1"}" data-group="${escapeHtml(group.id)}" type="button">${toggleLabel}</button>
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
    const action = button.dataset.ruleAction;
    try {
      if (action === "edit") return await openRuleGroupDrawer(group, false);
      if (action === "copy") return await openRuleGroupDrawer(group, true);
      if (action === "logs") return openRuleGroupLogs(group);
      if (action === "toggle") return await setFrontendRuleGroupEnabled(group, button.dataset.enabled === "1");
      if (action === "stop") return await emergencyStopFrontendRuleGroup(group);
      if (action === "delete") return await deleteFrontendRuleGroup(group);
    } catch (error) {
      toast(error.message || String(error), "error");
    }
  }
  function firstRuleBinding(group) {
    return (group.bindings || []).find(item => Array.isArray(item.rules) && item.rules.length) || (group.bindings || [])[0] || null;
  }
  function buildRuleGroupDraft(group, copy) {
    const first = group ? firstRuleBinding(group) : null;
    const strategy = (first && first.strategy) || {};
    const id = group && !copy ? group.id : newFrontendRuleGroupId();
    const products = group ? (group.products || []).filter(value => ALLOWED_PRODUCTS.includes(value)) : ["dramawave"];
    const rules = first && Array.isArray(first.rules) && first.rules.length ? first.rules : defaultCrossRegionRules();
    const defaultWindow = (first && first.rule_set_default_window) || { type: "since_start", hours: 24 };
    const selectedAccountKeys = new Set();
    if (group && !copy) {
      group.bindings.forEach(binding => {
        const ids = Array.isArray((binding.strategy || {}).selected_account_ids) ? binding.strategy.selected_account_ids : (binding.account_ids || []);
        ids.forEach(accountId => selectedAccountKeys.add(`${binding.product}:${String(accountId || "").replace(/^act_/, "")}`));
      });
    } else if (group && copy) {
      group.bindings.forEach(binding => {
        const ids = Array.isArray((binding.strategy || {}).selected_account_ids) ? binding.strategy.selected_account_ids : (binding.account_ids || []);
        ids.forEach(accountId => selectedAccountKeys.add(`${binding.product}:${String(accountId || "").replace(/^act_/, "")}`));
      });
    }
    return {
      mode: group && !copy ? "edit" : "create",
      id,
      originalId: group ? group.id : "",
      existingBindings: group && !copy ? group.bindings.slice() : [],
      name: group ? `${group.name}${copy ? " 副本" : ""}` : "+8非亚洲语种10点关停",
      description: group ? (group.description || strategy.description || "") : "账户时区为 +8，排除 JA/KO/ZHTW/TH/ID/VI/MS/TL 后，其他语种到关闭时间控停；不限制国家组，当天禁止重启。",
      products: products.length ? products : ["dramawave"],
      country_groups: group ? (Array.isArray(group.country_groups) ? group.country_groups : []) : [],
      close_time: group ? (group.close_time || strategy.close_time || "10:00") : "10:00",
      execute_timezone: group ? (group.execute_timezone || strategy.execute_timezone || "account") : "account",
      block_same_day_reopen: group ? group.block_same_day_reopen : true,
      allow_next_day_reopen: group ? group.allow_next_day_reopen : true,
      default_window: defaultWindow,
      rules,
      selectedAccountKeys,
    };
  }
  async function openRuleGroupDrawer(group, copy) {
    state.ruleGroupDraft = buildRuleGroupDraft(group, copy);
    $("ruleGroupDrawer").classList.remove("hidden");
    renderRuleGroupDrawer();
    await ensureRuleGroupAccountsForProducts(state.ruleGroupDraft.products);
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
    const builderRuleName = builderRule.name || "+8非亚洲语种10点关停";
    const builderAction = builderRule.action || "pause";
    const builderTimezones = conditionList(builderRule, ["account_time_zone", "time_zone", "timezone", "account_timezone"], ["in"], defaultTimezones);
    const builderLanguages = conditionList(builderRule, "language", ["not_in"], defaultExcludedLanguages);
    const builderCountries = (draft.country_groups || []).length ? draft.country_groups : conditionList(builderRule, ["country", "country_group", "geo", "region"], ["in"], []);
    const builderAgeHours = conditionNumber(builderRule, "age_hours", ["gte"], "");
    const builderSpendMin = conditionNumber(builderRule, "spend", ["gte", "gt"], "");
    const builderInstallMax = conditionNumber(builderRule, "install", ["lte", "lt"], "");
    const builderRoasMax = conditionNumber(builderRule, "roas_pct", ["lte", "lt"], "");
    const builderPurchaseMax = conditionNumber(builderRule, "purchase", ["lte", "lt"], "");
    const builderCpaMin = conditionNumber(builderRule, "purchase_cpa", ["gte", "gt"], "");
    $("drawerTitle").textContent = draft.mode === "edit" ? "编辑规则组" : "新建规则组";
    $("drawerSubTitle").textContent = draft.mode === "edit" ? `正在编辑 ${draft.id}` : "保存后会生成一个前端规则组，并按产品拆成底层配置";
    $("ruleGroupDrawerBody").innerHTML = `
      <section class="drawer-section">
        <div class="section-title"><span>1</span><h3>基础信息</h3></div>
        <div class="grid two"><div class="field"><label>规则组名称</label><input id="drawerGroupName" value="${escapeHtml(draft.name)}" /></div><div class="field"><label>规则组 ID</label><input id="drawerGroupId" value="${escapeHtml(draft.id)}" readonly /></div></div>
        <div class="field"><label>规则组说明</label><textarea id="drawerGroupDescription" class="short-textarea">${escapeHtml(draft.description)}</textarea></div>
      </section>
      <section class="drawer-section">
        <div class="section-title"><span>2</span><h3>产品与账号</h3></div>
        <div class="field"><label>产品（固定枚举，多选）</label><div class="check-list compact fixed-products" id="drawerProducts">${ALLOWED_PRODUCTS.map(value => `<label class="check-option"><input type="checkbox" value="${value}" ${draft.products.includes(value) ? "checked" : ""} /><span>${PRODUCT_LABELS[value]}</span></label>`).join("")}</div></div>
        <div class="grid"><div class="field"><label>账号搜索</label><input id="drawerAccountSearch" placeholder="搜索账号名 / ID；输入 act_... 可直接加入" /></div><div class="field"><label>时区筛选</label><input id="drawerTimezoneFilter" value="+8" /></div><div class="field"><label>&nbsp;</label><label class="check-inline"><input id="drawerPlus8Only" type="checkbox" checked /> 只看 +8</label></div><div class="field"><label>&nbsp;</label><div class="row"><button class="btn" id="drawerAddSearchAccount" type="button">加入搜索账号</button><button class="btn" id="drawerSelectVisibleAccounts" type="button">全选可见</button><button class="btn" id="drawerClearVisibleAccounts" type="button">清空可见</button></div></div></div>
        <div class="manual-account-box"><div class="field"><label>手动添加账号 ID</label><textarea id="drawerManualAccounts" class="short-textarea" placeholder="支持粘贴 act_1146901540906487、1026707669580137；多个账号用换行、逗号或空格分隔"></textarea><span class="hint">手动账号会加入当前选中的每个产品；适合账号未出现在接口列表时使用。</span></div><button class="btn" id="drawerAddManualAccounts" type="button">加入选中产品</button></div>
        <div class="selected-account-box"><div class="bulk-head"><strong>已选账号</strong><span class="hint" id="drawerSelectedAccountHint">0 个</span></div><div class="selected-account-list" id="drawerSelectedAccounts"></div></div>
        <div class="account-product-list" id="drawerAccounts"></div>
      </section>
      <section class="drawer-section">
        <div class="section-title"><span>3</span><h3>规则阈值</h3></div>
        <div class="grid"><div class="field"><label>规则名称</label><input id="builderRuleName" value="${escapeHtml(builderRuleName)}" /></div><div class="field"><label>动作</label><select id="builderAction"><option value="pause">pause 关闭</option><option value="observe">observe 观望</option></select></div><div class="field"><label>账户时区 in</label><input id="builderTimezones" value="${escapeHtml(builderTimezones.join(","))}" /></div><div class="field"><label>国家组 in（留空=不限）</label><input id="builderCountries" value="${escapeHtml(builderCountries.join(","))}" placeholder="留空表示所有国家组" /></div></div>
        <div class="grid"><div class="field"><label>排除语种 not in</label><input id="builderLanguages" value="${escapeHtml(builderLanguages.join(","))}" /></div><div class="field"><label>已运行小时 >=</label><input id="builderAgeHours" type="number" min="0" placeholder="可选" value="${escapeHtml(builderAgeHours)}" /></div><div class="field"><label>消耗 >=</label><input id="builderSpendMin" type="number" min="0" step="0.01" placeholder="可选" value="${escapeHtml(builderSpendMin)}" /></div><div class="field"><label>安装 <=</label><input id="builderInstallMax" type="number" min="0" placeholder="可选" value="${escapeHtml(builderInstallMax)}" /></div></div>
        <div class="grid"><div class="field"><label>ROAS% <=</label><input id="builderRoasMax" type="number" min="0" step="0.01" placeholder="可选" value="${escapeHtml(builderRoasMax)}" /></div><div class="field"><label>购物 <=</label><input id="builderPurchaseMax" type="number" min="0" placeholder="可选" value="${escapeHtml(builderPurchaseMax)}" /></div><div class="field"><label>Purchase CPA >=</label><input id="builderCpaMin" type="number" min="0" step="0.01" placeholder="可选" value="${escapeHtml(builderCpaMin)}" /></div><div class="field"><label>默认指标窗口</label><select id="drawerWindowType"><option value="since_start">起始至当前</option><option value="today">账户当天</option><option value="recent_hours">最近 N 小时</option></select></div></div>
        <div class="grid single"><div class="field"><label>N 小时</label><input id="drawerWindowHours" type="number" min="1" max="720" value="${escapeHtml(draft.default_window.hours || 24)}" /></div></div>
        <div class="row"><button class="btn" id="buildDrawerRuleBtn" type="button">按上方阈值生成规则 JSON</button><span class="hint">可视化阈值会覆盖下面 JSON；需要多条规则时可直接编辑 JSON。</span></div>
        <div class="field"><label>规则 JSON</label><textarea id="drawerRulesJson">${escapeHtml(JSON.stringify(draft.rules, null, 2))}</textarea><span class="hint">字段支持 age_hours、account_time_zone、language、country、spend、install、purchase、revenue、roas_pct、purchase_cpa、effective_status；国家组留空不会生成 country 条件。</span></div>
      </section>
      <section class="drawer-section">
        <div class="section-title"><span>4</span><h3>绑定策略</h3></div>
        <div class="grid"><div class="field"><label>关闭时间</label><input id="drawerCloseTime" type="time" value="${escapeHtml(draft.close_time)}" /></div><div class="field"><label>执行时区</label><input id="drawerExecuteTimezone" value="${escapeHtml(draft.execute_timezone)}" /></div><div class="field"><label>&nbsp;</label><label class="check-inline"><input id="drawerBlockSameDay" type="checkbox" ${draft.block_same_day_reopen ? "checked" : ""} /> 当天禁止重启</label></div><div class="field"><label>&nbsp;</label><label class="check-inline"><input id="drawerAllowNextDay" type="checkbox" ${draft.allow_next_day_reopen ? "checked" : ""} /> 隔天允许程序重启</label></div></div>
      </section>
      <section class="drawer-section">
        <div class="section-title"><span>5</span><h3>保存配置</h3></div>
        <div class="summary-box" id="drawerSummary"></div>
      </section>`;
    $("drawerWindowType").value = draft.default_window.type || "since_start";
    $("builderAction").value = builderAction;
    $("drawerProducts").onchange = async () => {
      captureDraftSelectedAccounts();
      const products = selectedProducts("drawerProducts");
      await ensureRuleGroupAccountsForProducts(products);
      renderRuleGroupAccounts();
      updateRuleGroupSummary();
    };
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
      $("drawerRulesJson").value = JSON.stringify(buildDrawerRulesFromThresholds(), null, 2);
      $("builderCountries").value = splitValues($("builderCountries").value).join(",");
      updateRuleGroupSummary();
      toast("已生成规则 JSON");
    };
  }
  function accountValue(item) {
    return normalizeAccountId((item && (item.account_id || item.ad_account_id)) || "");
  }
  function normalizeAccountId(value) {
    return String(value || "").trim().replace(/^act_/i, "");
  }
  async function ensureRuleGroupAccountsForProducts(products) {
    await Promise.all((products || []).filter(value => ALLOWED_PRODUCTS.includes(value)).map(async value => {
      if (state.ruleGroupAccounts[value]) return;
      try {
        const data = await api(`/api/ad-control/accounts?product=${encodeURIComponent(value)}`);
        state.ruleGroupAccounts[value] = data.items || [];
        if (data.warning) console.warn(`ad-control accounts ${value}: ${data.warning}`);
      } catch (error) {
        state.ruleGroupAccounts[value] = [];
        console.warn(`ad-control accounts ${value} failed`, error);
      }
    }));
  }
  function captureDraftSelectedAccounts() {
    const draft = state.ruleGroupDraft;
    if (!draft) return;
    document.querySelectorAll("[data-rule-account]").forEach(input => {
      if (input.checked) draft.selectedAccountKeys.add(input.dataset.accountKey);
      else draft.selectedAccountKeys.delete(input.dataset.accountKey);
    });
  }
  function ruleAccountVisible(account) {
    const query = ($("drawerAccountSearch")?.value || "").trim().toLowerCase();
    const tzFilter = ($("drawerTimezoneFilter")?.value || "").trim();
    const plus8Only = $("drawerPlus8Only")?.checked;
    const haystack = `${accountValue(account)} ${account.account_name || account.name || ""}`.toLowerCase();
    if (query && !haystack.includes(query)) return false;
    if (plus8Only && !isPlus8Timezone(account.time_zone)) return false;
    if (tzFilter && !Array.from(timezoneValues(account.time_zone)).some(item => timezoneValues(tzFilter).has(item))) return false;
    return true;
  }
  function searchAccountQuery() {
    return ($("drawerAccountSearch")?.value || "").trim().toLowerCase();
  }
  function accountMatchesSearch(productValue, accountId, display) {
    const query = searchAccountQuery();
    if (!query) return true;
    const normalizedQuery = normalizeAccountId(query);
    const haystack = `${productValue} ${accountId} ${display.title || ""} ${display.meta || ""}`.toLowerCase();
    return haystack.includes(query) || (!!normalizedQuery && String(accountId || "").toLowerCase().includes(normalizedQuery.toLowerCase()));
  }
  function renderRuleGroupAccounts() {
    const root = $("drawerAccounts");
    const draft = state.ruleGroupDraft;
    if (!root || !draft) return;
    captureDraftSelectedAccounts();
    const products = selectedProducts("drawerProducts");
    root.innerHTML = products.length ? products.map(productValue => {
      const accounts = state.ruleGroupAccounts[productValue] || [];
      const visible = accounts.filter(ruleAccountVisible);
      return `<div class="account-product-block"><div class="bulk-head"><strong>${escapeHtml(productValue)}</strong><span class="hint">${visible.length}/${accounts.length} 个账号</span></div><div class="account-list">${visible.length ? visible.map(account => {
        const id = accountValue(account);
        const key = `${productValue}:${id}`;
        const checked = draft.selectedAccountKeys.has(key) ? "checked" : "";
        return `<label class="account-option"><input type="checkbox" data-rule-account="1" data-account-key="${escapeHtml(key)}" value="${escapeHtml(id)}" ${checked} /><div class="account-title">${escapeHtml(account.account_name || account.name || id)}</div><div class="account-meta">${escapeHtml(id)} / ${escapeHtml(account.time_zone || "--")}</div></label>`;
      }).join("") : `<div class="empty">无匹配账号</div>`}</div></div>`;
    }).join("") : `<div class="empty">请先选择产品</div>`;
    renderSelectedRuleAccounts();
    updateRuleGroupSummary();
  }
  function addManualRuleAccounts() {
    const draft = state.ruleGroupDraft;
    if (!draft) return;
    const products = selectedProducts("drawerProducts");
    if (!products.length) return toast("请先选择产品", "error");
    const ids = Array.from(new Set(splitValues($("drawerManualAccounts").value).map(normalizeAccountId).filter(Boolean)));
    if (!ids.length) return toast("请粘贴账号 ID", "error");
    addRuleAccountIds(products, ids);
    $("drawerManualAccounts").value = "";
    renderRuleGroupAccounts();
    toast(`已加入 ${ids.length} 个账号到 ${products.length} 个产品`);
  }
  function addSearchRuleAccounts() {
    const products = selectedProducts("drawerProducts");
    if (!products.length) return toast("请先选择产品", "error");
    const ids = Array.from(new Set(splitValues($("drawerAccountSearch").value).map(normalizeAccountId).filter(Boolean)));
    if (!ids.length) return toast("请在账号搜索框输入 account_id", "error");
    addRuleAccountIds(products, ids);
    renderRuleGroupAccounts();
    toast(`已从搜索框加入 ${ids.length} 个账号`);
  }
  function addRuleAccountIds(products, ids) {
    const draft = state.ruleGroupDraft;
    if (!draft) return;
    (products || []).forEach(productValue => {
      (ids || []).forEach(id => draft.selectedAccountKeys.add(`${productValue}:${id}`));
    });
  }
  function selectedAccountDisplay(productValue, accountId) {
    const account = (state.ruleGroupAccounts[productValue] || []).find(item => accountValue(item) === accountId);
    if (!account) return { title: accountId, meta: "手动添加" };
    return {
      title: account.account_name || account.name || accountId,
      meta: `${accountId} / ${account.time_zone || "--"}`,
    };
  }
  function renderSelectedRuleAccounts() {
    const root = $("drawerSelectedAccounts");
    const hint = $("drawerSelectedAccountHint");
    const draft = state.ruleGroupDraft;
    if (!root || !draft) return;
    const products = selectedProducts("drawerProducts");
    const allSelected = Array.from(draft.selectedAccountKeys).filter(key => products.some(productValue => key.startsWith(`${productValue}:`))).sort();
    const selected = allSelected.filter(key => {
      const [productValue, accountId] = key.split(":", 2);
      return accountMatchesSearch(productValue, accountId, selectedAccountDisplay(productValue, accountId));
    });
    if (hint) hint.textContent = searchAccountQuery() ? `${selected.length}/${allSelected.length} 个` : `${allSelected.length} 个`;
    root.innerHTML = selected.length ? selected.map(key => {
      const [productValue, accountId] = key.split(":", 2);
      const display = selectedAccountDisplay(productValue, accountId);
      return `<div class="selected-account-item"><div><strong>${escapeHtml(productValue)}</strong><span>${escapeHtml(display.title)}</span><small>${escapeHtml(display.meta)}</small></div><button class="btn" data-remove-account-key="${escapeHtml(key)}" type="button">移除</button></div>`;
    }).join("") : (allSelected.length ? `<div class="empty compact-empty">已选账号中无匹配。可以清空搜索，或点“加入搜索账号”把当前输入加入选中产品。</div>` : `<div class="empty compact-empty">暂无已选账号，可从下方列表勾选或手动粘贴 account_id。</div>`);
  }
  function setVisibleRuleAccounts(checked) {
    const draft = state.ruleGroupDraft;
    if (!draft) return;
    document.querySelectorAll("[data-rule-account]").forEach(input => {
      input.checked = checked;
      if (checked) draft.selectedAccountKeys.add(input.dataset.accountKey);
      else draft.selectedAccountKeys.delete(input.dataset.accountKey);
    });
    renderSelectedRuleAccounts();
    updateRuleGroupSummary();
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
    return [{ name: $("builderRuleName").value.trim() || "+8非亚洲语种10点关停", action: $("builderAction").value || "pause", enabled: true, window: { type: $("drawerWindowType").value || "since_start" }, conditions }];
  }
  function readRuleGroupDraftFromDrawer() {
    const draft = state.ruleGroupDraft;
    captureDraftSelectedAccounts();
    let rules;
    try { rules = JSON.parse($("drawerRulesJson").value || "[]"); } catch (error) { throw new Error("规则 JSON 格式错误"); }
    if (!Array.isArray(rules) || !rules.length) throw new Error("至少需要一条规则");
    const products = selectedProducts("drawerProducts");
    if (!products.length) throw new Error("请选择至少一个产品");
    const accountsByProduct = {};
    products.forEach(productValue => {
      accountsByProduct[productValue] = Array.from(draft.selectedAccountKeys)
        .filter(key => key.startsWith(`${productValue}:`))
        .map(key => key.slice(productValue.length + 1))
        .filter(Boolean);
    });
    const missingProduct = products.find(productValue => !accountsByProduct[productValue].length);
    if (missingProduct) throw new Error(`${missingProduct} 还没有选择账号`);
    return {
      id: draft.id,
      name: $("drawerGroupName").value.trim(),
      description: $("drawerGroupDescription").value.trim(),
      products,
      accountsByProduct,
      country_groups: splitValues($("builderCountries").value),
      rules,
      default_window: { type: $("drawerWindowType").value || "since_start", hours: Number($("drawerWindowHours").value || 24) },
      close_time: $("drawerCloseTime").value || "",
      execute_timezone: $("drawerExecuteTimezone").value || "account",
      block_same_day_reopen: $("drawerBlockSameDay").checked,
      allow_next_day_reopen: $("drawerAllowNextDay").checked,
    };
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
    const accountTotal = Object.values(summary.accountsByProduct).reduce((total, ids) => total + ids.length, 0);
    box.innerHTML = `<div class="summary-grid"><div><span>产品</span><strong>${summary.products.map(value => PRODUCT_LABELS[value]).join(" / ")}</strong></div><div><span>账号</span><strong>${accountTotal} 个</strong></div><div><span>规则</span><strong>${summary.rules.length} 条</strong></div><div><span>策略</span><strong>${escapeHtml(strategySummary(summary))}</strong></div></div>
      <div class="hint">保存后写入 ad_control_rule_set、ad_control_account_group、ad_control_rule_group；所有底层 binding 默认 disabled。</div>`;
  }
  async function saveFrontendRuleGroup() {
    let draft;
    try { draft = readRuleGroupDraftFromDrawer(); } catch (error) { toast(error.message, "error"); return; }
    if (!draft.name) return toast("请填写规则组名称", "error");
    try {
      saveCountryGroups(draft.country_groups);
      const existingByProduct = new Map((state.ruleGroupDraft.existingBindings || []).map(binding => [binding.product, binding]));
      const removedBindings = (state.ruleGroupDraft.existingBindings || []).filter(binding => !draft.products.includes(binding.product));
      for (const binding of removedBindings) {
        await api(`/api/ad-control/bindings/${encodeURIComponent(bindingId(binding))}`, { method: "DELETE" });
      }
      const savedBindings = [];
      for (const productValue of draft.products) {
        const existing = existingByProduct.get(productValue) || {};
        const ruleSetId = existing.rule_set_id || ruleGroupRuleSetId(draft.id, productValue);
        const accountGroupId = existing.account_group_id || ruleGroupAccountGroupId(draft.id, productValue);
        const currentBindingId = bindingId(existing) || ruleGroupBindingId(draft.id, productValue);
        const accountIds = draft.accountsByProduct[productValue] || [];
        await api("/api/ad-control/rule-sets", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rule_set_id: ruleSetId, product: productValue, name: `${draft.name} / ${productValue}`, rules: draft.rules, default_window: draft.default_window }),
        });
        await api("/api/ad-control/account-groups", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ group_id: accountGroupId, product: productValue, name: `${draft.name} / ${productValue} / 账号池`, account_ids: accountIds }),
        });
        const binding = await api("/api/ad-control/bindings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            group_id: currentBindingId,
            product: productValue,
            name: `${draft.name} / ${productValue}`,
            account_group_id: accountGroupId,
            rule_set_id: ruleSetId,
            enabled: false,
            strategy: {
              frontend_rule_group_id: draft.id,
              frontend_rule_group_name: draft.name,
              selected_products: draft.products,
              selected_account_ids: accountIds,
              account_count: accountIds.length,
              country_groups: draft.country_groups,
              close_time: draft.close_time,
              execute_timezone: draft.execute_timezone,
              block_same_day_reopen: draft.block_same_day_reopen,
              allow_next_day_reopen: draft.allow_next_day_reopen,
              description: draft.description,
            },
          }),
        });
        savedBindings.push(binding);
      }
      toast(`规则组已保存：${savedBindings.length} 个产品绑定，默认禁用`);
      closeRuleGroupDrawer();
      await refreshRuleGroups();
    } catch (error) {
      toast(error.message || String(error), "error");
    }
  }
  async function setFrontendRuleGroupEnabled(group, enabled) {
    const label = enabled ? "启用" : "禁用";
    const enableMessage = group.emergency_stopped
      ? "当前规则组已急停。启用会校验所有账户的 Token 权限，并解除急停后重新启用，确认继续？"
      : "启用会校验当前账户池及所有账号的 Token 权限，通过后直接启用规则组，确认继续？";
    if (enabled && !confirm(enableMessage)) return;
    const targets = (group.bindings || []).map(binding => ({ binding, id: bindingId(binding) })).filter(item => item.id);
    const results = await Promise.allSettled(targets.map(item => api(`/api/ad-control/bindings/${encodeURIComponent(item.id)}/enabled`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) })));
    const detailed = results.map((result, index) => ({ result, id: targets[index].id, product: targets[index].binding.product || "" }));
    const failed = detailed.filter(item => item.result.status === "rejected");
    const ok = results.length - failed.length;
    if (failed.length) {
      if (enabled && ok) {
        const succeeded = detailed.filter(item => item.result.status === "fulfilled");
        await Promise.allSettled(succeeded.map(item => api(`/api/ad-control/bindings/${encodeURIComponent(item.id)}/enabled`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: false }) })));
      }
      console.warn("ad-control toggle failed", failed.map(item => ({ id: item.id, product: item.product, error: item.result.reason && item.result.reason.message ? item.result.reason.message : String(item.result.reason) })));
    } else {
      toast(`${label}完成：${ok} 个底层绑定`);
    }
    await refreshRuleGroups();
    if (failed.length) {
      const reasons = failed.map(item => `${item.product || item.id}：${item.result.reason?.message || String(item.result.reason)}`);
      throw new Error(`${label}失败；已保持整组禁用。${reasons.join("；")}`);
    }
  }
  async function emergencyStopFrontendRuleGroup(group) {
    if (!confirm("确认急停当前规则组？只停止该规则组的底层绑定，不会主动改广告状态。")) return;
    for (const binding of group.bindings) {
      await api("/api/ad-control/emergency-stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope: "rule_group", group_id: bindingId(binding) }) });
    }
    toast("当前规则组已急停");
    await refreshRuleGroups();
  }
  async function deleteFrontendRuleGroup(group) {
    if (!confirm("确认删除当前规则组？历史执行日志不会删除。")) return;
    for (const binding of group.bindings) {
      await api(`/api/ad-control/bindings/${encodeURIComponent(bindingId(binding))}`, { method: "DELETE" });
    }
    toast("规则组已删除");
    await refreshRuleGroups();
  }
  function openRuleGroupLogs(group) {
    const binding = group.bindings[0];
    if (!binding) return toast("规则组没有日志绑定", "error");
    window.location.assign(`/ad-control-logs.html?product=${encodeURIComponent(binding.product || "")}&binding_id=${encodeURIComponent(bindingId(binding))}`);
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
    $("pageRoot").innerHTML = `<section class="panel"><div class="panel-head"><h2>运行控制台已下线</h2></div><div class="panel-body"><div class="risk">规则组启停已统一收口到规则组管理页，不再要求 Preview，也不再从此页面手动执行。</div><a class="btn primary" href="/ad-control-rules.html">返回规则组管理</a></div></section>`;
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
    $("pageRoot").innerHTML = `${productFilter(`<div class="field"><label>绑定关系</label><select id="bindingFilter"><option value="">全部绑定</option></select></div>`)}<section class="panel"><div class="panel-head"><div><h2>执行日志</h2><span class="hint" id="logStorageHint">正在读取 ads_ai 调控日志...</span></div><button class="btn" id="loadLogsBtn">查询</button></div><div class="panel-body"><div class="grid"><div class="field"><label>展示方式</label><select id="viewFilter"><option value="daily">按业务日汇总</option><option value="raw">原始批次</option></select></div><div class="field"><label>动作</label><select id="actionFilter"><option value="">全部执行动作</option><option value="pause">关停 pause</option><option value="reopen">重启 reopen</option></select></div><div class="field"><label>开始日期</label><input id="dateFrom" type="date" /></div><div class="field"><label>结束日期</label><input id="dateTo" type="date" /></div><div class="field"><label>显示数量</label><input id="limitInput" type="number" min="1" max="200" value="50" /></div></div><div class="list" id="actionList"></div></div></section>`;
    const params = new URLSearchParams(window.location.search);
    await loadProducts();
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
    $("bindingFilter").innerHTML = optionHtml(state.bindings, "binding_id", "name", "全部绑定");
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
