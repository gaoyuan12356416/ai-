(function () {
  const DEFAULT_NAV = [
    {
      key: "ad_control",
      label: "AI自动规则调控",
      order: 5,
      module: "ad_control_center",
      items: [
        {
          key: "adControl",
          label: "调控概览",
          description: "查看状态、风险提示和常用入口",
          kind: "page",
          href: "/ad-control.html",
          module: "ad_control_center",
          enabled: true,
          order: 10,
        },
        {
          key: "adControlRules",
          label: "规则集",
          description: "创建和维护可复用调控规则",
          kind: "page",
          href: "/ad-control-rules.html",
          module: "ad_control_center",
          enabled: true,
          order: 20,
        },
        {
          key: "adControlPools",
          label: "账户池",
          description: "按产品维护账户池",
          kind: "page",
          href: "/ad-control-account-pools.html",
          module: "ad_control_center",
          enabled: true,
          order: 30,
        },
        {
          key: "adControlBindings",
          label: "绑定关系",
          description: "绑定产品、账户池和规则集",
          kind: "page",
          href: "/ad-control-bindings.html",
          module: "ad_control_center",
          enabled: true,
          order: 40,
        },
        {
          key: "adControlRun",
          label: "运行控制台",
          description: "Preview、dry-run、确认关闭和急停",
          kind: "page",
          href: "/ad-control-run.html",
          module: "ad_control_center",
          enabled: true,
          order: 50,
        },
        {
          key: "adControlTokens",
          label: "Token配置",
          description: "配置产品默认和账户级 token",
          kind: "page",
          href: "/ad-control-tokens.html",
          module: "ad_control_center",
          enabled: true,
          order: 60,
        },
        {
          key: "adControlLogs",
          label: "执行日志",
          description: "查看调控审计和失败原因",
          kind: "page",
          href: "/ad-control-logs.html",
          module: "ad_control_center",
          enabled: true,
          order: 70,
        },
      ],
    },
    {
      key: "drama",
      label: "短剧任务列表",
      order: 10,
      module: "drama_synthesis",
      items: [
        {
          key: "tasks",
          label: "剧集合成",
          description: "创建、重试、删除、查看结果",
          kind: "page",
          href: "/drama-synthesis.html",
          module: "drama_synthesis",
          enabled: true,
          order: 10,
        },
        {
          key: "screenshots",
          label: "封面图合成",
          description: "批量提交剧 ID，查看图片进度",
          kind: "page",
          href: "/screenshots.html",
          module: "cover_synthesis",
          enabled: true,
          order: 20,
        },
      ],
    },
    {
      key: "ad_material",
      label: "投放素材",
      order: 20,
      module: "ad_material_tasks",
      items: [
        {
          key: "adMaterials",
          label: "投放素材任务",
          description: "创建需求、审核素材并完成上报",
          kind: "page",
          href: "/ad-material-tasks.html",
          module: "ad_material_tasks",
          enabled: true,
          order: 10,
        },
      ],
    },
    {
      key: "ad_material_test",
      label: "投放素材测试",
      order: 21,
      module: "ad_material_test",
      items: [
        {
          key: "adMaterialTest",
          label: "投放素材测试环境",
          description: "独立测试服务，不影响线上任务",
          kind: "page",
          href: "https://ai.yingliangads.com/ad-material-test/#adMaterials",
          module: "ad_material_test",
          enabled: true,
          order: 10,
        },
      ],
    },
    {
      key: "voiceover",
      label: "配音剧素材",
      order: 30,
      module: "voiceover_drama_tasks",
      items: [
        {
          key: "voiceoverTasks",
          label: "配音剧语种任务",
          description: "查询系列素材并批量创建设计师需求",
          kind: "page",
          href: "/voiceover-drama.html",
          module: "voiceover_drama_tasks",
          enabled: true,
          order: 10,
        },
      ],
    },
    {
      key: "system",
      label: "设置",
      order: 90,
      adminOnly: true,
      items: [
        {
          key: "settings",
          label: "基础设置",
          description: "产品映射和系统说明",
          kind: "page",
          href: "/settings.html",
          adminOnly: true,
          enabled: true,
          order: 10,
        },
        {
          key: "navigation",
          label: "快速导航栏配置",
          description: "维护后台左侧导航",
          kind: "page",
          href: "/navigation.html",
          adminOnly: true,
          enabled: true,
          order: 15,
        },
        {
          key: "users",
          label: "用户管理",
          description: "查看登录用户和权限",
          kind: "page",
          href: "/users.html",
          adminOnly: true,
          enabled: true,
          order: 20,
        },
        {
          key: "logs",
          label: "操作日志",
          description: "仅管理员可见",
          kind: "page",
          href: "/logs.html",
          adminOnly: true,
          enabled: true,
          order: 30,
        },
      ],
    },
  ];

  const CONFIG_CACHE_KEY = "quickNavConfigCache";
  const AUTH_CACHE_KEY = "dramaAdminAuthCache";
  let navCache = null;
  let styleInjected = false;
  const collapsedGroups = new Set();

  function injectStyle() {
    if (styleInjected) return;
    styleInjected = true;
    const style = document.createElement("style");
    style.textContent = `
      .quick-nav-root,
      .nav { display: grid; gap: 10px; }
      .quick-nav-root .nav-group,
      .nav .nav-group {
        display: grid;
        gap: 6px;
        border-radius: 16px;
        padding: 6px;
        background: rgba(255,255,255,.04);
      }
      .quick-nav-root .nav-parent,
      .nav .nav-parent {
        width: 100%;
        min-height: 36px;
        border: 0;
        background: transparent;
        display: flex;
        align-items: center;
        justify-content: space-between;
        color: rgba(237,243,255,.86);
        font: inherit;
        font-weight: 800;
        padding: 10px 12px;
        cursor: pointer;
        border-radius: 12px;
        text-align: left;
      }
      .quick-nav-root .nav-parent:hover,
      .nav .nav-parent:hover { background: rgba(255,255,255,.07); }
      .quick-nav-root .nav-parent::after,
      .nav .nav-parent::after {
        content: "\\203A";
        color: rgba(237,243,255,.62);
        font-size: 20px;
        line-height: 1;
        transition: transform .18s ease;
        transform: rotate(90deg);
      }
      .quick-nav-root .nav-group.collapsed .nav-parent::after,
      .nav .nav-group.collapsed .nav-parent::after { transform: rotate(0deg); }
      .quick-nav-root .nav-children,
      .nav .nav-children { display: grid; gap: 6px; padding-left: 8px; }
      .quick-nav-root .nav-group.collapsed .nav-children,
      .nav .nav-group.collapsed .nav-children { display: none; }
      .quick-nav-root .nav-item,
      .nav .nav-item {
        width: 100%;
        border: 0;
        background: transparent;
        color: inherit;
        text-align: left;
        border-radius: 14px;
        padding: 12px 14px 12px 18px;
        cursor: pointer;
        display: block;
        text-decoration: none;
      }
      .quick-nav-root .nav-item:hover,
      .nav .nav-item:hover,
      .quick-nav-root .nav-item.active,
      .nav .nav-item.active {
        background: linear-gradient(135deg, rgba(47,102,255,.96), rgba(31,72,201,.96));
        box-shadow: 0 12px 24px rgba(23, 50, 131, .28);
        color: #fff;
      }
      .quick-nav-root .nav-item span,
      .nav .nav-item span {
        display: block;
        margin-top: 4px;
        color: rgba(237,243,255,.64);
        font-size: 12px;
        line-height: 1.35;
      }
      .quick-nav-root .nav-item strong,
      .nav .nav-item strong { display: block; font-size: 14px; line-height: 1.25; }
    `;
    document.head.appendChild(style);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function userCan(auth, moduleKey) {
    if (!moduleKey) return true;
    const user = auth && auth.user;
    if (!user) return false;
    if (user.is_admin) return true;
    return !!(user.permissions && user.permissions[moduleKey]);
  }

  function visibleFor(auth, item) {
    const user = auth && auth.user;
    if (item.enabled === false) return false;
    if (item.adminOnly && !(user && user.is_admin)) return false;
    if (item.module && !userCan(auth, item.module)) return false;
    return true;
  }

  function sortItems(items) {
    return (items || []).slice().sort((a, b) => (a.order || 0) - (b.order || 0));
  }

  const INTERNAL_VIEW_HREFS = {
    tasks: "/drama-synthesis.html",
    screenshots: "/screenshots.html",
    adMaterials: "/ad-material-tasks.html",
    voiceoverTasks: "/voiceover-drama.html",
    adControl: "/ad-control.html",
    adControlRules: "/ad-control-rules.html",
    adControlPools: "/ad-control-account-pools.html",
    adControlBindings: "/ad-control-bindings.html",
    adControlRun: "/ad-control-run.html",
    adControlTokens: "/ad-control-tokens.html",
    adControlLogs: "/ad-control-logs.html",
    settings: "/settings.html",
    navigation: "/navigation.html",
    users: "/users.html",
    logs: "/logs.html",
  };

  function navItemHref(item) {
    const key = item.view || item.key;
    return INTERNAL_VIEW_HREFS[key] || item.href || "#";
  }

  function cloneNav(config) {
    return JSON.parse(JSON.stringify(Array.isArray(config) ? config : []));
  }

  function normalizeNavConfig(config) {
    if (!Array.isArray(config)) return null;
    return cloneNav(config);
  }

  function readStoredConfig() {
    try {
      const raw = localStorage.getItem(CONFIG_CACHE_KEY);
      const cached = raw ? JSON.parse(raw) : null;
      return normalizeNavConfig(cached && cached.items);
    } catch (error) {
      try { localStorage.removeItem(CONFIG_CACHE_KEY); } catch (storageError) {}
      return null;
    }
  }

  function writeStoredConfig(config) {
    try {
      const items = normalizeNavConfig(config);
      if (!items) return;
      localStorage.setItem(CONFIG_CACHE_KEY, JSON.stringify({ items, updatedAt: Date.now() }));
    } catch (error) {}
  }

  function readStoredAuth() {
    try {
      const raw = localStorage.getItem(AUTH_CACHE_KEY);
      const cached = raw ? JSON.parse(raw) : null;
      if (!cached || cached.expiresAt <= Date.now()) return null;
      return cached.auth || null;
    } catch (error) {
      try { localStorage.removeItem(AUTH_CACHE_KEY); } catch (storageError) {}
      return null;
    }
  }

  function writeStoredAuth(auth) {
    if (!(auth && auth.authenticated && auth.user)) {
      try { localStorage.removeItem(AUTH_CACHE_KEY); } catch (error) {}
      return;
    }
    try {
      localStorage.setItem(AUTH_CACHE_KEY, JSON.stringify({
        auth,
        authenticated: true,
        user: auth.user,
        expiresAt: Date.now() + 6 * 60 * 60 * 1000,
      }));
    } catch (error) {}
  }

  function renderOptions(options) {
    const merged = Object.assign({}, options || {});
    if (!merged.auth) merged.auth = readStoredAuth() || {};
    else writeStoredAuth(merged.auth);
    return merged;
  }

  async function loadConfig() {
    if (navCache) return navCache;
    try {
      const response = await fetch("/navigation.json", { cache: "no-store", credentials: "same-origin" });
      if (!response.ok) throw new Error("navigation config request failed");
      const config = normalizeNavConfig(await response.json());
      if (!config) throw new Error("navigation config must be an array");
      navCache = config;
      writeStoredConfig(config);
    } catch (error) {
      navCache = readStoredConfig() || cloneNav(DEFAULT_NAV);
    }
    return navCache;
  }

  function itemLookup(config) {
    const lookup = {};
    for (const group of config || []) {
      for (const item of group.items || []) {
        lookup[item.key] = item;
      }
    }
    return lookup;
  }

  function renderItem(item, activeKey) {
    const active = item.key === activeKey || item.view === activeKey;
    const description = item.description ? `<span>${escapeHtml(item.description)}</span>` : "";
    return `<a class="nav-item ${active ? "active" : ""}" data-quick-nav-key="${escapeHtml(item.key)}" href="${escapeHtml(navItemHref(item))}"><strong>${escapeHtml(item.label || "")}</strong>${description}</a>`;
  }

  function renderGroup(group, items, activeKey) {
    const activeInGroup = items.some(item => item.key === activeKey || item.view === activeKey);
    if (activeInGroup) collapsedGroups.delete(group.key);
    const collapsed = collapsedGroups.has(group.key) ? " collapsed" : "";
    return `<div class="nav-group${collapsed}" data-quick-nav-group="${escapeHtml(group.key)}"><button class="nav-parent" type="button" data-quick-nav-toggle="${escapeHtml(group.key)}">${escapeHtml(group.label || "")}</button><div class="nav-children">${items.map(item => renderItem(item, activeKey)).join("")}</div></div>`;
  }

  function buildNavHtml(config, options) {
    return sortItems(config).map(group => {
      if (!visibleFor(options.auth || {}, group)) return "";
      const items = sortItems(group.items).filter(item => visibleFor(options.auth || {}, item));
      if (!items.length) return "";
      return renderGroup(group, items, options.activeKey);
    }).join("");
  }

  function setActive(container, activeKey) {
    const root = typeof container === "string" ? document.querySelector(container) : container;
    if (!root) return;
    root.querySelectorAll(".nav-item").forEach(item => {
      item.classList.toggle("active", item.dataset.quickNavKey === activeKey);
    });
    const activeItem = Array.from(root.querySelectorAll(".nav-item"))
      .find(item => item.dataset.quickNavKey === activeKey);
    activeItem?.closest(".nav-group")?.classList.remove("collapsed");
  }

  function bindEvents(container, config, options) {
    container.onclick = event => {
      const toggle = event.target.closest("[data-quick-nav-toggle]");
      if (toggle && container.contains(toggle)) {
        const key = toggle.dataset.quickNavToggle || "";
        if (collapsedGroups.has(key)) collapsedGroups.delete(key);
        else collapsedGroups.add(key);
        toggle.closest(".nav-group")?.classList.toggle("collapsed", collapsedGroups.has(key));
        return;
      }
      const link = event.target.closest("a.nav-item");
      if (!link || !container.contains(link) || typeof options.onNavigate !== "function") return;
      const items = itemLookup(config);
      const item = items[link.dataset.quickNavKey] || null;
      if (!item) return;
      event.preventDefault();
      options.onNavigate(item, event);
    };
  }

  async function render(options) {
    const container = typeof options.container === "string" ? document.querySelector(options.container) : options.container;
    if (!container) return;
    injectStyle();
    container.classList.add("quick-nav-root");
    const initialOptions = renderOptions(options);
    const initialConfig = navCache || readStoredConfig() || DEFAULT_NAV;
    container.innerHTML = buildNavHtml(initialConfig, initialOptions);
    bindEvents(container, initialConfig, initialOptions);
    const config = await loadConfig();
    const finalOptions = renderOptions(options);
    container.innerHTML = buildNavHtml(config, finalOptions);
    bindEvents(container, config, finalOptions);
  }

  function clearCache() {
    navCache = null;
    try { localStorage.removeItem(CONFIG_CACHE_KEY); } catch (error) {}
  }

  window.QuickNav = { render, loadConfig, clearCache, setActive };
})();
