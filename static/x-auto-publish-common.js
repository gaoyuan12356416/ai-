(function () {
  "use strict";

  const API_BASE = "/api/admin/x-auto-publish";
  let toastTimer = 0;
  let confirmResolver = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function show(node) {
    if (node) node.classList.remove("hidden");
  }

  function hide(node) {
    if (node) node.classList.add("hidden");
  }

  function setText(node, value, fallback) {
    if (!node) return;
    const normalized = value == null || value === "" ? (fallback == null ? "—" : fallback) : value;
    node.textContent = String(normalized);
  }

  function clear(node) {
    if (node) node.replaceChildren();
  }

  function element(tag, options) {
    const node = document.createElement(tag);
    const config = options || {};
    if (config.className) node.className = config.className;
    if (config.text != null) node.textContent = String(config.text);
    if (config.type) node.type = config.type;
    if (config.href) node.href = config.href;
    if (config.title) node.title = config.title;
    if (config.attributes) {
      Object.entries(config.attributes).forEach(([name, value]) => {
        if (value != null) node.setAttribute(name, String(value));
      });
    }
    if (config.dataset) {
      Object.entries(config.dataset).forEach(([name, value]) => {
        if (value != null) node.dataset[name] = String(value);
      });
    }
    return node;
  }

  function appendTextCell(row, value, className) {
    const cell = element("td", { className: className || "" });
    setText(cell, value);
    row.appendChild(cell);
    return cell;
  }

  function objectValue(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function readItem(payload, keys) {
    const source = objectValue(payload);
    for (const key of keys || []) {
      if (source[key] && typeof source[key] === "object" && !Array.isArray(source[key])) return source[key];
    }
    if (source.data && typeof source.data === "object" && !Array.isArray(source.data)) return source.data;
    return source;
  }

  function readItems(payload, keys) {
    const source = objectValue(payload);
    for (const key of keys || []) {
      if (Array.isArray(source[key])) return source[key];
    }
    if (source.data && Array.isArray(source.data.items)) return source.data.items;
    if (Array.isArray(source.data)) return source.data;
    return [];
  }

  function queryString(values) {
    const params = new URLSearchParams();
    Object.entries(values || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && String(value) !== "") params.set(key, String(value));
    });
    return params.toString();
  }

  async function api(path, options) {
    const config = Object.assign({
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "application/json" },
    }, options || {});
    config.headers = Object.assign({}, config.headers || {});
    if (config.body && !config.headers["Content-Type"]) {
      config.headers["Content-Type"] = "application/json; charset=UTF-8";
    }
    const response = await fetch(path, config);
    const raw = await response.text();
    let payload = {};
    try {
      payload = raw ? JSON.parse(raw) : {};
    } catch (_) {
      payload = {};
    }
    if (!response.ok) {
      const nestedError = objectValue(payload.error);
      const error = new Error(nestedError.message || payload.message || payload.error_message || (typeof payload.error === "string" ? payload.error : "") || `请求失败（HTTP ${response.status}）`);
      error.code = nestedError.code || payload.code || (typeof payload.error === "string" ? payload.error : "") || "request_failed";
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function boolValue(value) {
    return value === true || value === 1 || value === "1" || value === "true" || value === "enabled";
  }

  function numberValue(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function positiveId(value) {
    const normalized = String(value == null ? "" : value).trim();
    return /^[1-9][0-9]*$/.test(normalized) ? normalized : "";
  }

  function formatTime(value) {
    if (!value) return "—";
    const raw = String(value);
    const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(raw) ? raw : raw.replace(" ", "T") + "Z";
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return raw;
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function formatNumber(value, digits) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "—";
    return new Intl.NumberFormat("zh-CN", {
      minimumFractionDigits: 0,
      maximumFractionDigits: digits == null ? 2 : digits,
    }).format(parsed);
  }

  function statusBadge(label, kind) {
    return element("span", { className: `badge${kind ? ` ${kind}` : ""}`, text: label });
  }

  function statusKind(status) {
    const value = String(status || "").toLowerCase();
    if (["enabled", "published", "completed", "success", "succeeded", "ready"].includes(value)) return "success";
    if (["failed", "error", "blocked", "canceled", "cancelled", "partial_failed"].includes(value)) return "danger";
    if (["running", "queued", "pending", "selecting", "reserved", "preparing", "retry_wait", "publishing", "reconciling", "scheduled"].includes(value)) return "info";
    if (["needs_review", "unknown", "no_candidate", "skipped", "disabled", "paused", "hold"].includes(value)) return "warning";
    return "";
  }

  function showToast(message, isError) {
    const toast = byId("toast");
    if (!toast) return;
    setText(toast, message, "");
    toast.classList.toggle("error", !!isError);
    show(toast);
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => hide(toast), 4500);
  }

  function settleConfirm(value) {
    const resolver = confirmResolver;
    confirmResolver = null;
    if (resolver) resolver(!!value);
  }

  function ensureConfirmBindings() {
    const dialog = byId("confirmDialog");
    if (!dialog || dialog.dataset.bound === "1") return;
    dialog.dataset.bound = "1";
    byId("confirmCancel")?.addEventListener("click", () => {
      if (typeof dialog.close === "function") dialog.close("cancel");
      else dialog.removeAttribute("open");
      settleConfirm(false);
    });
    byId("confirmAccept")?.addEventListener("click", () => {
      if (typeof dialog.close === "function") dialog.close("accept");
      else dialog.removeAttribute("open");
      settleConfirm(true);
    });
    dialog.addEventListener("cancel", event => {
      event.preventDefault();
      if (typeof dialog.close === "function") dialog.close("cancel");
      else dialog.removeAttribute("open");
      settleConfirm(false);
    });
    dialog.addEventListener("close", () => {
      if (dialog.returnValue !== "accept") settleConfirm(false);
    });
  }

  function confirmAction(options) {
    ensureConfirmBindings();
    const dialog = byId("confirmDialog");
    if (!dialog) return Promise.resolve(false);
    if (confirmResolver) settleConfirm(false);
    const config = options || {};
    setText(byId("confirmTitle"), config.title || "请确认操作", "");
    setText(byId("confirmMessage"), config.message || "确认继续吗？", "");
    const accept = byId("confirmAccept");
    setText(accept, config.confirmText || "确认", "");
    accept.classList.toggle("danger", !!config.danger);
    accept.classList.toggle("primary", !config.danger);
    dialog.returnValue = "";
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    return new Promise(resolve => {
      confirmResolver = resolve;
    });
  }

  function pageIdFromQuery(name) {
    return positiveId(new URLSearchParams(location.search).get(name || "id"));
  }

  async function initShell(options) {
    const config = options || {};
    let auth;
    try {
      auth = await api("/api/ui/topbar");
    } catch (_) {
      auth = { authenticated: false, login_url: "/api/auth/feishu/login" };
    }
    await window.QuickNav.render({
      container: "#quickNav",
      auth,
      activeKey: config.activeKey || "xAutoPublishTemplates",
    });
    window.UiTopbar.render({
      auth,
      userCard: "#userCard",
      authButton: "#authButton",
      refreshButton: "#refreshPage",
    });
    byId("authButton")?.addEventListener("click", () => window.UiTopbar.handleAuthAction({ auth, api }));
    const user = auth && auth.user;
    if (!(auth && auth.authenticated && user)) {
      show(byId("loginGate"));
      return { allowed: false, auth };
    }
    const allowed = !!(user.is_admin || (user.permissions && user.permissions.x_accounts));
    if (!allowed) {
      show(byId("permissionGate"));
      return { allowed: false, auth };
    }
    show(byId("pageRoot"));
    ensureConfirmBindings();
    if (typeof config.onReady === "function") await config.onReady(auth);
    return { allowed: true, auth };
  }

  window.XAutoPublish = {
    API_BASE,
    api,
    appendTextCell,
    boolValue,
    byId,
    clear,
    confirmAction,
    element,
    formatNumber,
    formatTime,
    hide,
    initShell,
    numberValue,
    objectValue,
    pageIdFromQuery,
    positiveId,
    queryString,
    readItem,
    readItems,
    setText,
    show,
    showToast,
    statusBadge,
    statusKind,
  };
})();
