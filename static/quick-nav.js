(function () {
  const DEFAULT_NAV = [
    {
      key: "drama",
      label: "剧集合成",
      module: "drama_synthesis",
      items: [
        { key: "tasks", label: "任务列表", description: "创建、重试、删除、查看结果", kind: "page", href: "/drama-synthesis.html", module: "drama_synthesis", enabled: true, order: 10 },
        { key: "screenshots", label: "截图素材", description: "批量提交剧 ID，查看图片进度", kind: "page", href: "/screenshots.html", module: "cover_synthesis", enabled: true, order: 20 }
      ]
    },
    {
      key: "voiceover",
      label: "配音剧素材",
      module: "voiceover_drama_tasks",
      items: [
        { key: "voiceoverTasks", label: "配音剧语种任务", description: "查询系列素材并批量创建设计师需求", kind: "page", href: "/voiceover-drama.html", module: "voiceover_drama_tasks", enabled: true, order: 10 }
      ]
    },
    {
      key: "ad_material",
      label: "投放素材",
      module: "ad_material_tasks",
      items: [
        { key: "adMaterials", label: "投放素材任务", description: "创建需求、审核素材并完成上报", kind: "page", href: "/ad-material-tasks.html", module: "ad_material_tasks", enabled: true, order: 10 }
      ]
    },
    {
      key: "system",
      label: "设置",
      adminOnly: true,
      items: [
        { key: "settings", label: "基础设置", description: "产品映射和系统说明", kind: "page", href: "/settings.html", adminOnly: true, enabled: true, order: 10 },
        { key: "navigation", label: "快速导航栏配置", description: "维护后台左侧导航", kind: "page", href: "/navigation.html", adminOnly: true, enabled: true, order: 15 },
        { key: "users", label: "用户管理", description: "查看登录用户和权限", kind: "page", href: "/users.html", adminOnly: true, enabled: true, order: 20 },
        { key: "logs", label: "操作日志", description: "仅管理员可见", kind: "page", href: "/logs.html", adminOnly: true, enabled: true, order: 30 }
      ]
    }
  ];

  let navCache = null;
  let styleInjected = false;

  function injectStyle() {
    if (styleInjected) return;
    styleInjected = true;
    const style = document.createElement("style");
    style.textContent = `
      .quick-nav-root .nav-group, .nav .nav-group { display: grid; gap: 8px; margin-bottom: 14px; }
      .quick-nav-root .nav-parent, .nav .nav-parent { color: rgba(237,243,255,.66); font-size: 12px; font-weight: 700; padding: 6px 10px; }
      .quick-nav-root .nav-children, .nav .nav-children { display: grid; gap: 6px; }
      .quick-nav-root .nav-item, .nav .nav-item { width: 100%; border: 0; background: transparent; color: inherit; text-align: left; border-radius: 14px; padding: 12px 14px; cursor: pointer; display: block; text-decoration: none; }
      .quick-nav-root .nav-item:hover, .nav .nav-item:hover, .quick-nav-root .nav-item.active, .nav .nav-item.active { background: linear-gradient(135deg, rgba(47,102,255,.96), rgba(31,72,201,.96)); box-shadow: 0 12px 24px rgba(23, 50, 131, .28); color: #fff; }
      .quick-nav-root .nav-item span, .nav .nav-item span { display: block; margin-top: 4px; color: rgba(237,243,255,.64); font-size: 12px; }
      .quick-nav-root .nav-item strong, .nav .nav-item strong { display: block; font-size: 14px; }
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
    voiceoverTasks: "/voiceover-drama.html",
    adMaterials: "/ad-material-tasks.html",
    settings: "#settings",
    navigation: "#navigation",
    users: "#users",
    logs: "#logs"
  };

  function navItemHref(item) {
    const key = item.view || item.key;
    return INTERNAL_VIEW_HREFS[key] || item.href || "#";
  }

  function mergeDefaultNav(config) {
    const groups = Array.isArray(config) ? config.slice() : [];
    for (const defaultGroup of DEFAULT_NAV) {
      const group = groups.find(item => item.key === defaultGroup.key);
      if (!group) {
        groups.push(defaultGroup);
        continue;
      }
      const existing = new Set((group.items || []).map(item => item.key));
      for (const defaultItem of defaultGroup.items || []) {
        if (!existing.has(defaultItem.key)) {
          group.items = group.items || [];
          group.items.push(defaultItem);
        }
      }
    }
    return groups;
  }

  async function loadConfig() {
    if (navCache) return navCache;
    try {
      const response = await fetch("/navigation.json", { cache: "no-store", credentials: "same-origin" });
      if (!response.ok) throw new Error("navigation config request failed");
      navCache = mergeDefaultNav(await response.json());
    } catch (error) {
      navCache = DEFAULT_NAV;
    }
    return navCache;
  }

  function renderItem(item, activeKey) {
    const active = item.key === activeKey || item.view === activeKey;
    const description = item.description ? `<span>${escapeHtml(item.description)}</span>` : "";
    return `<a class="nav-item ${active ? "active" : ""}" data-quick-nav-key="${escapeHtml(item.key)}" href="${escapeHtml(navItemHref(item))}"><strong>${escapeHtml(item.label || "")}</strong>${description}</a>`;
  }

  function buildNavHtml(config, options) {
    return sortItems(config).map(group => {
      if (!visibleFor(options.auth || {}, group)) return "";
      const items = sortItems(group.items).filter(item => visibleFor(options.auth || {}, item));
      if (!items.length) return "";
      return `<div class="nav-group"><div class="nav-parent">${escapeHtml(group.label || "")}</div><div class="nav-children">${items.map(item => renderItem(item, options.activeKey)).join("")}</div></div>`;
    }).join("");
  }

  async function render(options) {
    const container = typeof options.container === "string" ? document.querySelector(options.container) : options.container;
    if (!container) return;
    injectStyle();
    container.classList.add("quick-nav-root");
    container.innerHTML = buildNavHtml(navCache || DEFAULT_NAV, options);
    const config = await loadConfig();
    container.innerHTML = buildNavHtml(config, options);
  }

  function clearCache() {
    navCache = null;
  }

  window.QuickNav = { render, loadConfig, clearCache };
})();
