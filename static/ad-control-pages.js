(function () {
  const PAGE = document.body.dataset.page || "overview";
  const TITLES = {
    overview: ["AI自动规则调控", "查看调控中心状态、风险提示和常用入口。", "adControl"],
    rules: ["规则集", "创建和维护可复用的调控规则集，不绑定账户。", "adControlRules"],
    pools: ["账户池", "按产品创建账户池，后续在绑定关系中复用。", "adControlPools"],
    bindings: ["绑定关系", "配置产品、账户池和规则集的绑定，并控制启停。", "adControlBindings"],
    run: ["运行控制台", "选择绑定关系后执行 live preview、dry-run、确认关闭和急停。", "adControlRun"],
    tokens: ["Token配置", "配置产品默认 token 和账户级 token override。", "adControlTokens"],
    logs: ["执行日志", "查看 preview 和 execute 的审计结果、跳过原因和错误原因。", "adControlLogs"],
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
  const state = { auth: null, products: [], accounts: [], pools: [], ruleSets: [], bindings: [], preview: null };
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
    if (!response.ok) throw new Error(data.message || data.error || "请求失败");
    return data;
  }
  function hasPermission(auth) {
    const user = auth && auth.user;
    return !!(user && (user.is_admin || (user.permissions && user.permissions.ad_control_center)));
  }
  function product() {
    return ($("productSelect") || {}).value || "";
  }
  function selectedAccounts() {
    return Array.from(document.querySelectorAll("#accountList input[type=checkbox]:checked")).map(item => item.value);
  }
  function money(value) {
    return Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
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
    const data = await api("/api/ad-control/products");
    state.products = data.items || [];
    const select = $("productSelect");
    if (select) {
      const previous = select.value;
      select.innerHTML = state.products.map(item => `<option value="${escapeHtml(item.product)}">${escapeHtml(item.product)}${item.app_package ? " / " + escapeHtml(item.app_package) : ""}</option>`).join("");
      if (previous) select.value = previous;
    }
  }
  async function loadAccounts() {
    if (!product()) return;
    const data = await api(`/api/ad-control/accounts?product=${encodeURIComponent(product())}`);
    state.accounts = data.items || [];
    renderAccounts([]);
  }
  function renderAccounts(selected) {
    const list = $("accountList");
    if (!list) return;
    const selectedSet = new Set(selected || []);
    list.innerHTML = state.accounts.length ? state.accounts.map(item => {
      const id = item.account_id || item.ad_account_id || "";
      const checked = selectedSet.has(id) ? "checked" : "";
      return `<label class="account-option"><input type="checkbox" value="${escapeHtml(id)}" ${checked} /><div class="account-title">${escapeHtml(item.account_name || item.name || id)}</div><div class="account-meta">${escapeHtml(id)} / ${escapeHtml(item.time_zone || "--")}</div></label>`;
    }).join("") : `<div class="empty">暂无账户</div>`;
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
        <div class="risk">拆页后请按顺序配置：Token配置 -> 账户池 -> 规则集 -> 绑定关系 -> 运行控制台。默认不会启用任何新绑定，也不会自动关闭广告。</div>
      </div></section>
      <section class="panel"><div class="panel-head"><h2>快速入口</h2></div><div class="panel-body"><div class="cards">
        ${quickCard("规则集", "/ad-control-rules.html", "维护可复用规则")}
        ${quickCard("账户池", "/ad-control-account-pools.html", "选择产品账户")}
        ${quickCard("绑定关系", "/ad-control-bindings.html", "绑定账户池与规则集")}
        ${quickCard("运行控制台", "/ad-control-run.html", "Preview、dry-run、确认关闭")}
        ${quickCard("Token配置", "/ad-control-tokens.html", "配置产品和账户 token")}
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
    $("pageRoot").innerHTML = `${productFilter()}<section class="panel"><div class="panel-head"><h2>编辑规则集</h2><div class="row"><button class="btn" id="newBtn">新建</button><button class="btn primary" id="saveBtn">保存规则集</button></div></div><div class="panel-body">
      <div class="grid"><div class="field"><label>规则集名称</label><input id="nameInput" placeholder="7-8 小时低效关停" /></div><div class="field"><label>规则集 ID</label><input id="idInput" readonly /></div><div class="field"><label>默认指标窗口</label><select id="windowType"><option value="since_start">起始至当前</option><option value="today">账户当天</option><option value="recent_hours">最近 N 小时</option></select></div><div class="field"><label>N 小时</label><input id="windowHours" type="number" min="1" max="720" value="24" /></div></div>
      <div class="field"><label>规则 JSON</label><textarea id="rulesInput"></textarea><span class="hint">字段支持 age_hours、spend、install、purchase、revenue、roas_pct、purchase_cpa、effective_status；操作符支持 gt/gte/lt/lte/eq/between/in。</span></div>
      </div></section><section class="panel"><div class="panel-head"><h2>规则集列表</h2></div><div class="panel-body"><div class="list" id="ruleSetList"></div></div></section>`;
    await loadProducts(); await loadRuleSets(); fillRuleSet(state.ruleSets[0] || null); renderRuleSetList();
    $("productSelect").onchange = async () => { await loadRuleSets(); fillRuleSet(null); renderRuleSetList(); };
    $("newBtn").onclick = () => fillRuleSet(null);
    $("saveBtn").onclick = saveRuleSet;
  }
  function fillRuleSet(item) {
    $("idInput").value = item ? item.rule_set_id : "";
    $("nameInput").value = item ? item.name : "";
    const win = (item && item.default_window) || { type: "since_start", hours: 24 };
    $("windowType").value = win.type || "since_start";
    $("windowHours").value = win.hours || 24;
    $("rulesInput").value = JSON.stringify(item ? (item.rules || []) : defaultRules, null, 2);
  }
  async function saveRuleSet() {
    let rules;
    try { rules = JSON.parse($("rulesInput").value || "[]"); } catch (error) { toast("规则 JSON 格式错误", "error"); return; }
    await api("/api/ad-control/rule-sets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rule_set_id: $("idInput").value, product: product(), name: $("nameInput").value.trim(), rules, default_window: { type: $("windowType").value, hours: Number($("windowHours").value || 24) } }) });
    toast("规则集已保存"); await loadRuleSets(); renderRuleSetList();
  }
  function renderRuleSetList() {
    $("ruleSetList").innerHTML = state.ruleSets.length ? state.ruleSets.map(item => `<div class="item"><div><strong>${escapeHtml(item.name)}</strong><span class="hint">${escapeHtml(item.rule_set_id)} / ${escapeHtml((item.default_window || {}).type || "since_start")} / ${item.rules.length} 条规则</span></div><div class="row"><button class="btn" data-edit-rule-set="${escapeHtml(item.rule_set_id)}">编辑</button><button class="btn danger" data-delete-rule-set="${escapeHtml(item.rule_set_id)}">删除</button></div></div>`).join("") : `<div class="empty">暂无规则集</div>`;
    $("ruleSetList").onclick = async event => {
      const edit = event.target.closest("[data-edit-rule-set]");
      const del = event.target.closest("[data-delete-rule-set]");
      if (edit) fillRuleSet(state.ruleSets.find(item => item.rule_set_id === edit.dataset.editRuleSet));
      if (del && confirm("确认删除规则集？已被绑定使用的规则集不能删除。")) { await api(`/api/ad-control/rule-sets/${encodeURIComponent(del.dataset.deleteRuleSet)}`, { method: "DELETE" }); await loadRuleSets(); renderRuleSetList(); }
    };
  }

  async function renderPools() {
    $("pageRoot").innerHTML = `${productFilter()}<section class="panel"><div class="panel-head"><h2>账户池</h2><button class="btn primary" id="savePoolBtn">保存账户池</button></div><div class="panel-body">
      <div class="grid two"><div class="field"><label>账户池名称</label><input id="poolName" placeholder="北美 +8 调控账户" /></div><div class="field"><label>账户池 ID</label><input id="poolId" readonly /></div></div>
      <div class="row"><button class="btn" id="selectAllBtn">全选账户</button><button class="btn" id="clearBtn">清空账户</button><span class="hint" id="accountHint"></span></div><div class="account-list" id="accountList"></div>
      </div></section><section class="panel"><div class="panel-head"><h2>账户池列表</h2></div><div class="panel-body"><div class="list" id="poolList"></div></div></section>`;
    await loadProducts(); await refreshPoolPage();
    $("productSelect").onchange = refreshPoolPage;
    $("savePoolBtn").onclick = savePool;
    $("selectAllBtn").onclick = () => document.querySelectorAll("#accountList input").forEach(input => input.checked = true);
    $("clearBtn").onclick = () => document.querySelectorAll("#accountList input").forEach(input => input.checked = false);
  }
  async function refreshPoolPage() {
    await loadAccounts(); await loadPools(); $("accountHint").textContent = `已加载 ${state.accounts.length} 个账户`; renderPoolList();
  }
  async function savePool() {
    await api("/api/ad-control/account-groups", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ group_id: $("poolId").value, product: product(), name: $("poolName").value.trim(), account_ids: selectedAccounts() }) });
    toast("账户池已保存"); $("poolId").value = ""; $("poolName").value = ""; await refreshPoolPage();
  }
  function renderPoolList() {
    $("poolList").innerHTML = state.pools.length ? state.pools.map(item => `<div class="item"><div><strong>${escapeHtml(item.name)}</strong><span class="hint">${escapeHtml(item.group_id)} / ${item.account_ids.length} 个账户</span></div><div class="row"><button class="btn" data-edit-pool="${escapeHtml(item.group_id)}">编辑</button><button class="btn danger" data-delete-pool="${escapeHtml(item.group_id)}">删除</button></div></div>`).join("") : `<div class="empty">暂无账户池</div>`;
    $("poolList").onclick = async event => {
      const edit = event.target.closest("[data-edit-pool]");
      const del = event.target.closest("[data-delete-pool]");
      if (edit) { const item = state.pools.find(pool => pool.group_id === edit.dataset.editPool); $("poolId").value = item.group_id; $("poolName").value = item.name; renderAccounts(item.account_ids); }
      if (del && confirm("确认删除账户池？")) { await api(`/api/ad-control/account-groups/${encodeURIComponent(del.dataset.deletePool)}`, { method: "DELETE" }); await refreshPoolPage(); }
    };
  }

  async function renderBindings() {
    $("pageRoot").innerHTML = `${productFilter()}<section class="panel"><div class="panel-head"><h2>绑定关系</h2><div class="row"><button class="btn" id="newBindingBtn">新建</button><button class="btn primary" id="saveBindingBtn">保存绑定</button></div></div><div class="panel-body">
      <div class="grid"><div class="field"><label>绑定名称</label><input id="bindingName" placeholder="北美账户池 + 7小时规则" /></div><div class="field"><label>绑定 ID</label><input id="bindingId" readonly /></div><div class="field"><label>账户池</label><select id="poolSelect"></select></div><div class="field"><label>规则集</label><select id="ruleSetSelect"></select></div></div>
      <div class="risk">新绑定默认禁用。启用前必须在运行控制台完成 live preview，且 token 校验通过。</div></div></section><section class="panel"><div class="panel-head"><h2>绑定列表</h2></div><div class="panel-body"><div class="list" id="bindingList"></div></div></section>`;
    await loadProducts(); await refreshBindingPage();
    $("productSelect").onchange = refreshBindingPage;
    $("newBindingBtn").onclick = () => fillBinding(null);
    $("saveBindingBtn").onclick = saveBinding;
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
  }
  async function saveBinding() {
    await api("/api/ad-control/bindings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ group_id: $("bindingId").value, product: product(), name: $("bindingName").value.trim(), account_group_id: $("poolSelect").value, rule_set_id: $("ruleSetSelect").value, enabled: false }) });
    toast("绑定已保存，默认禁用"); await refreshBindingPage();
  }
  function renderBindingList() {
    $("bindingList").innerHTML = state.bindings.length ? state.bindings.map(item => `<div class="item"><div><strong>${escapeHtml(item.name)}</strong><span class="hint">${escapeHtml(item.binding_id)} / 账户池 ${escapeHtml(item.account_group_id || "--")} / 规则集 ${escapeHtml(item.rule_set_name || item.rule_set_id || "--")} / ${item.enabled ? "已启用" : "已禁用"} / ${item.emergency_stopped ? "已急停" : "正常"}</span></div><div class="row"><button class="btn" data-edit-binding="${escapeHtml(item.binding_id)}">编辑</button><button class="btn" data-toggle-binding="${escapeHtml(item.binding_id)}" data-enabled="${item.enabled ? "0" : "1"}">${item.enabled ? "禁用" : "启用"}</button><button class="btn danger" data-delete-binding="${escapeHtml(item.binding_id)}">删除</button></div></div>`).join("") : `<div class="empty">暂无绑定</div>`;
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
    $("pageRoot").innerHTML = `${productFilter()}<section class="panel"><div class="panel-head"><h2>运行控制台</h2><div class="row"><button class="btn primary" id="previewBtn">Live Preview</button><button class="btn" id="dryRunBtn" disabled>Dry-run执行</button><button class="btn danger" id="executeBtn" disabled>确认关闭</button><button class="btn danger" id="stopBtn">急停</button></div></div><div class="panel-body">
      <div class="grid three"><div class="field"><label>绑定关系</label><select id="bindingSelect"></select></div><div class="field"><label>指标窗口</label><select id="windowType"><option value="">使用规则集默认</option><option value="since_start">起始至当前</option><option value="today">账户当天</option><option value="recent_hours">最近 N 小时</option></select></div><div class="field"><label>N 小时</label><input id="windowHours" type="number" min="1" max="720" value="24" /></div></div>
      <span class="hint" id="previewMeta">尚未 preview</span><div class="cards"><div class="metric"><span>命中 campaign</span><strong id="previewTotal">0</strong></div><div class="metric"><span>待关闭</span><strong id="previewPause">0</strong></div><div class="metric"><span>观察</span><strong id="previewObserve">0</strong></div><div class="metric"><span>异常</span><strong id="previewErrors">0</strong></div></div>
      <div class="table-wrap"><table><thead><tr><th>账户</th><th>Campaign</th><th>状态</th><th>起始/运行</th><th>实时指标</th><th>命中规则</th><th>动作</th></tr></thead><tbody id="previewRows"></tbody></table></div>
      </div></section><section class="panel"><div class="panel-head"><h2>刷新 campaign 起始时间缓存</h2></div><div class="panel-body"><div class="grid"><div class="field"><label>账户 ID</label><input id="refreshAccount" /></div><div class="field"><label>Campaign ID</label><input id="refreshCampaign" /></div><div class="field"><label>&nbsp;</label><button class="btn" id="refreshStartBtn">刷新缓存</button></div></div><div class="hint" id="refreshStartResult"></div></div></section>`;
    await loadProducts(); await refreshRunBindings();
    $("productSelect").onchange = refreshRunBindings;
    $("previewBtn").onclick = previewLive;
    $("dryRunBtn").onclick = () => executeLive(true);
    $("executeBtn").onclick = () => executeLive(false);
    $("stopBtn").onclick = emergencyStop;
    $("refreshStartBtn").onclick = refreshCampaignStart;
  }
  async function refreshRunBindings() {
    await loadBindings();
    $("bindingSelect").innerHTML = optionHtml(state.bindings, "binding_id", "name", "请选择绑定关系");
  }
  function selectedWindow() {
    return $("windowType").value ? { type: $("windowType").value, hours: Number($("windowHours").value || 24) } : null;
  }
  async function previewLive() {
    const bindingId = $("bindingSelect").value;
    if (!bindingId) return toast("请选择绑定关系", "error");
    const body = {};
    if (selectedWindow()) body.window = selectedWindow();
    const data = await api(`/api/ad-control/bindings/${encodeURIComponent(bindingId)}/preview-live`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    state.preview = data; renderPreview(data); toast("live preview 完成");
  }
  function renderPreview(data) {
    $("previewTotal").textContent = data.total || 0;
    $("previewPause").textContent = data.pause_count || 0;
    $("previewObserve").textContent = data.observe_count || 0;
    $("previewErrors").textContent = data.error_count || 0;
    $("previewMeta").textContent = `preview ${data.preview_id || "--"} / 过期 ${data.expires_at || "--"} / 剩余未展示 ${data.remaining_count || 0}`;
    $("dryRunBtn").disabled = !data.preview_id;
    $("executeBtn").disabled = !data.preview_id || !(data.pause_count > 0);
    $("previewRows").innerHTML = (data.items || []).map(item => {
      const m = item.metrics || {};
      const rules = (item.matched_rules || []).map(rule => rule.name || rule.action).join(", ");
      const targetCls = item.target_action === "pause" ? "danger" : (item.target_action === "observe" ? "warn" : "");
      return `<tr><td><div class="mono">${escapeHtml(item.account_id)}</div><div class="hint">token ${escapeHtml(item.token_user_id || "--")}</div></td><td><div>${escapeHtml(item.campaign_name || "--")}</div><div class="mono">${escapeHtml(item.campaign_id)}</div></td><td><span class="badge ok">${escapeHtml(item.effective_status || item.status || "--")}</span></td><td>${escapeHtml(item.campaign_start_at || "--")}<div class="hint">${item.age_hours == null ? "缺起始时间" : item.age_hours.toFixed(1) + " 小时"}</div></td><td>Spend ${money(m.spend)} / Install ${m.install || 0}<br>Purchase ${m.purchase || 0} / ROAS% ${money(m.roas_pct)} / CPA ${m.purchase_cpa == null ? "--" : money(m.purchase_cpa)}</td><td>${escapeHtml(rules || item.skip_reason || "--")}</td><td><span class="badge ${targetCls}">${escapeHtml(item.target_action || "none")}</span></td></tr>`;
    }).join("");
  }
  async function executeLive(dryRun) {
    if (!state.preview || !state.preview.preview_id) return toast("请先 live preview", "error");
    const body = { preview_id: state.preview.preview_id, preview_hash: state.preview.preview_hash, dry_run: !!dryRun };
    if (!dryRun) {
      const confirmText = window.prompt("真实关闭会调用 Meta API。请输入 EXECUTE_LIVE_PAUSE 确认：");
      if (confirmText !== "EXECUTE_LIVE_PAUSE") return;
      body.confirm = "EXECUTE_LIVE_PAUSE";
    }
    const data = await api(`/api/ad-control/bindings/${encodeURIComponent($("bindingSelect").value)}/execute-live`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`${dryRun ? "dry-run" : "真实执行"}完成：成功 ${data.success_count}，跳过 ${data.skipped_count}，失败 ${data.error_count}`);
  }
  async function emergencyStop() {
    const bindingId = $("bindingSelect").value;
    if (!confirm("确认急停？急停只停止绑定/runner，不主动修改广告状态。")) return;
    await api("/api/ad-control/emergency-stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(bindingId ? { scope: "rule_group", group_id: bindingId } : { scope: "global" }) });
    toast("已急停"); await refreshRunBindings();
  }
  async function refreshCampaignStart() {
    const data = await api("/api/ad-control/campaign-start/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ product: product(), account_id: $("refreshAccount").value, campaign_id: $("refreshCampaign").value }) });
    $("refreshStartResult").textContent = JSON.stringify(data);
  }

  async function renderTokens() {
    $("pageRoot").innerHTML = `${productFilter()}<section class="panel"><div class="panel-head"><h2>Token配置</h2><button class="btn" id="reloadBtn">刷新配置</button></div><div class="panel-body"><div class="grid"><div class="field"><label>配置范围</label><select id="tokenScope"><option value="">产品默认 token</option></select></div><div class="field"><label>token owner user_id</label><input id="tokenUserId" placeholder="ads_facebook_info.user_id" /></div><div class="field"><label>备注</label><input id="tokenLabel" placeholder="例如 Dramawave 控制 token" /></div><div class="field"><label>&nbsp;</label><div class="row"><button class="btn" id="validateBtn">校验</button><button class="btn primary" id="saveTokenBtn">保存</button></div></div></div><div class="list" id="tokenList"></div></div></section>`;
    await loadProducts(); await refreshTokenPage();
    $("productSelect").onchange = refreshTokenPage;
    $("reloadBtn").onclick = refreshTokenPage;
    $("validateBtn").onclick = validateToken;
    $("saveTokenBtn").onclick = saveToken;
  }
  async function refreshTokenPage() {
    await loadAccounts();
    $("tokenScope").innerHTML = `<option value="">产品默认 token</option>` + state.accounts.map(item => `<option value="${escapeHtml(item.account_id || "")}">${escapeHtml(item.account_name || item.account_id || "")} / ${escapeHtml(item.account_id || "")}</option>`).join("");
    const data = await api(`/api/ad-control/token-config?product=${encodeURIComponent(product())}`);
    $("tokenList").innerHTML = (data.items || []).length ? data.items.map(item => `<div class="item"><div><strong>${item.scope === "product" ? "产品默认 token" : escapeHtml(item.account_id)}</strong><span class="hint">owner ${escapeHtml(item.user_id)} / ${escapeHtml(item.label || "--")} / 最近校验 ${escapeHtml((item.validation || {}).validated_at || "--")}</span></div><span class="badge ${((item.validation || {}).ok) ? "ok" : "warn"}">${((item.validation || {}).ok) ? "校验通过" : "未校验或失败"}</span></div>`).join("") : `<div class="empty">暂无 token 配置</div>`;
  }
  async function validateToken() {
    const accounts = $("tokenScope").value ? [$("tokenScope").value] : state.accounts.map(item => item.account_id).filter(Boolean);
    const data = await api("/api/ad-control/token-config/validate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ product: product(), user_id: $("tokenUserId").value.trim(), accounts }) });
    toast(`校验完成：${data.ok_count}/${data.checked_count} 通过`);
  }
  async function saveToken() {
    await api("/api/ad-control/token-config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ product: product(), account_id: $("tokenScope").value, user_id: $("tokenUserId").value.trim(), label: $("tokenLabel").value.trim() }) });
    toast("token 配置已保存"); await refreshTokenPage();
  }

  async function renderLogs() {
    $("pageRoot").innerHTML = `${productFilter(`<div class="field"><label>绑定关系</label><select id="bindingFilter"><option value="">全部绑定</option></select></div>`)}<section class="panel"><div class="panel-head"><h2>执行日志</h2><button class="btn" id="loadLogsBtn">查询</button></div><div class="panel-body"><div class="grid"><div class="field"><label>动作</label><select id="actionFilter"><option value="">全部</option><option value="pause">pause</option><option value="preview">preview</option></select></div><div class="field"><label>开始日期</label><input id="dateFrom" type="date" /></div><div class="field"><label>结束日期</label><input id="dateTo" type="date" /></div><div class="field"><label>条数</label><input id="limitInput" type="number" min="1" max="200" value="50" /></div></div><div class="list" id="actionList"></div></div></section>`;
    await loadProducts(); await refreshLogBindings(); await loadLogs();
    $("productSelect").onchange = async () => { await refreshLogBindings(); await loadLogs(); };
    $("loadLogsBtn").onclick = loadLogs;
  }
  async function refreshLogBindings() {
    await loadBindings();
    $("bindingFilter").innerHTML = optionHtml(state.bindings, "binding_id", "name", "全部绑定");
  }
  async function loadLogs() {
    const qs = new URLSearchParams({ product: product(), binding_id: $("bindingFilter").value || "", action: $("actionFilter").value || "", date_from: $("dateFrom").value || "", date_to: $("dateTo").value || "", limit: $("limitInput").value || "50" });
    const data = await api(`/api/ad-control/actions?${qs.toString()}`);
    renderActionList(data.items || [], $("actionList"));
  }
  function renderActionList(items, node) {
    node.innerHTML = items.length ? items.map(item => `<div class="item"><div><strong>${escapeHtml(item.action_id)}</strong><span class="hint">${escapeHtml(item.created_at)} / ${escapeHtml(item.product)} / 绑定 ${escapeHtml(item.binding_id || "--")} / ${item.dry_run ? "dry-run" : "real"} / 成功 ${item.success_count} 跳过 ${item.skipped_count} 失败 ${item.error_count}</span><details><summary>结果详情</summary><pre class="mono">${escapeHtml(JSON.stringify(item.results || [], null, 2))}</pre></details></div><span class="badge">${escapeHtml(item.action)}</span></div>`).join("") : `<div class="empty">暂无执行日志</div>`;
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
