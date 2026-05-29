(function () {
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function loginUrl(auth) {
    const base = (auth && auth.login_url) || "/api/auth/feishu/login";
    const next = `${location.pathname || "/"}${location.search || ""}`;
    return `${base}${base.includes("?") ? "&" : "?"}next=${encodeURIComponent(next)}`;
  }

  function renderUserCard(card, auth) {
    if (!card) return;
    const user = auth && auth.user;
    if (!(auth && auth.authenticated && user)) {
      card.innerHTML = `<div class="user-avatar">未</div><div class="user-meta"><strong>未登录</strong><span>请先登录</span></div>`;
      return;
    }
    const name = user.name || user.email || "已登录";
    const meta = `${user.role || "user"} / tenant ${user.tenant_key || ""}`;
    const avatar = user.avatar_url
      ? `<img src="${escapeHtml(user.avatar_url)}" alt="${escapeHtml(name)}" loading="lazy" decoding="async" />`
      : escapeHtml(String(name).slice(0, 1) || "管");
    card.innerHTML = `<div class="user-avatar">${avatar}</div><div class="user-meta"><strong>${escapeHtml(name)}</strong><span>${escapeHtml(meta)}</span></div>`;
  }

  function render(options) {
    const auth = options.auth || {};
    renderUserCard(resolveElement(options.userCard), auth);
    const authButton = resolveElement(options.authButton || options.loginButton || options.logoutButton);
    if (authButton) {
      authButton.textContent = auth.authenticated ? (options.logoutText || "退出登录") : (options.loginText || "登录");
      authButton.dataset.topbarAction = auth.authenticated ? "logout" : "login";
    }
    const refreshButton = resolveElement(options.refreshButton);
    if (refreshButton && !refreshButton.dataset.topbarBound) {
      refreshButton.dataset.topbarBound = "1";
      refreshButton.addEventListener("click", () => location.reload());
    }
  }

  function resolveElement(element) {
    if (!element) return null;
    if (typeof element === "string") return document.querySelector(element);
    return element;
  }

  async function handleAuthAction(options) {
    const auth = options.auth || {};
    if (!auth.authenticated) {
      location.href = loginUrl(auth);
      return;
    }
    if (typeof options.api === "function") {
      await options.api("/api/auth/logout", { method: "POST", body: "{}" }).catch(() => {});
    } else {
      await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin", body: "{}" }).catch(() => {});
    }
    if (typeof options.afterLogout === "function") options.afterLogout();
    else location.reload();
  }

  window.UiTopbar = { render, loginUrl, handleAuthAction };
})();
