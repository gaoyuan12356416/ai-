(() => {
  "use strict";

  const API_BASE = "/api/admin/fb-auto-publish";
  const byId = id => document.getElementById(id);

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || "请求失败");
    return data;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  function positiveId(value) {
    const number = Number(value);
    return Number.isInteger(number) && number > 0 ? number : 0;
  }

  function templateVersion(item) {
    return positiveId(item && (item.version || item.current_version));
  }

  function readItem(payload) {
    if (!payload || typeof payload !== "object") return {};
    return payload.template || payload.item || {};
  }

  function operationId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return String(Date.now()) + "-" + Math.random().toString(16).slice(2);
  }

  let toastTimer = 0;
  function showToast(message, isError = false) {
    const toast = byId("toast");
    if (!toast) return;
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.className = "toast" + (isError ? " error" : "");
    toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 4200);
  }

  function confirmAction(title, message) {
    const dialog = byId("confirmDialog");
    if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(window.confirm(message));
    byId("confirmTitle").textContent = title;
    byId("confirmMessage").textContent = message;
    return new Promise(resolve => {
      const onCancel = event => {
        event.preventDefault();
        finish(false);
      };
      const finish = accepted => {
        dialog.removeEventListener("cancel", onCancel);
        dialog.close();
        byId("confirmCancel").onclick = null;
        byId("confirmAccept").onclick = null;
        resolve(accepted);
      };
      byId("confirmCancel").onclick = () => finish(false);
      byId("confirmAccept").onclick = () => finish(true);
      dialog.addEventListener("cancel", onCancel);
      dialog.showModal();
    });
  }

  async function boot(options) {
    let auth;
    try {
      auth = await api("/api/ui/topbar");
    } catch (_error) {
      auth = { authenticated: false };
    }
    await QuickNav.render({
      container: "#quickNav",
      auth,
      activeKey: "fbAutoPublishTemplates",
    });
    UiTopbar.render({
      auth,
      userCard: "#userCard",
      authButton: "#authButton",
      refreshButton: "#refreshPage",
    });
    byId("authButton").onclick = () => UiTopbar.handleAuthAction({ auth, api });
    if (!auth.authenticated) {
      byId("loginGate").classList.remove("hidden");
      return;
    }
    const user = auth.user || {};
    if (!(user.is_admin || (user.permissions && user.permissions.fb_page_posts))) {
      byId("permissionGate").classList.remove("hidden");
      return;
    }
    byId("pageRoot").classList.remove("hidden");
    try {
      await options.onReady(auth);
    } catch (error) {
      showToast(error.message || "页面数据加载失败", true);
    }
  }

  window.FBAutoPublishUI = {
    API_BASE,
    api,
    boot,
    byId,
    confirmAction,
    escapeHtml,
    operationId,
    positiveId,
    readItem,
    showToast,
    templateVersion,
  };
})();
