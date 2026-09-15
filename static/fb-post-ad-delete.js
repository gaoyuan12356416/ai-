(function () {
  const state = { auth: null, job: null, poll: null };
  const $ = id => document.getElementById(id);
  const escapeHtml = value => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  async function api(path, options) {
    const res = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || data.error || `HTTP ${res.status}`);
    return data;
  }

  function toast(message) {
    const el = $("toast");
    el.textContent = message;
    el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), 2200);
  }

  function renderStats(summary) {
    const s = summary || {};
    const cells = [
      ["Ads", s.ads || 0],
      ["Campaigns", s.campaigns || 0],
      ["账号", s.accounts || 0],
      ["唯一 Post", s.post_objects || 0],
      ["本地已删 Ads", s.local_deleted_ads || 0],
      ["错误", (s.post_errors || 0) + (s.ad_errors || 0)],
    ];
    $("stats").innerHTML = cells.map(([label, value]) => `<div class="stat"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`).join("");
  }

  function postText(item, field) {
    return (item.posts || []).map(post => post[field]).filter(Boolean).join(" ; ");
  }

  function renderDetails(items) {
    const list = (items || []).slice(0, 1000);
    $("detailCount").textContent = items && items.length > 1000 ? `显示前 1000 / 共 ${items.length}` : `共 ${items ? items.length : 0}`;
    $("detailBody").innerHTML = list.map(item => {
      const url = (item.post_preview_urls || [])[0] || "";
      const status = [
        item.local_status ? `ad:${item.local_status}` : "",
        postText(item, "delete_status") ? `post:${postText(item, "delete_status")}` : "",
        item.ad_delete_status ? `delete:${item.ad_delete_status}` : "",
      ].filter(Boolean).join(" / ");
      return `<tr>
        <td>${escapeHtml((item.series_ids || []).join(", "))}</td>
        <td>${escapeHtml(item.account_name || item.ad_account_id || "")}</td>
        <td class="mono">${escapeHtml(item.campaign_id || "")}<br>${escapeHtml(item.campaign_name || "")}</td>
        <td class="mono">${escapeHtml(item.ad_id || "")}</td>
        <td class="mono">${escapeHtml(postText(item, "post_id"))}</td>
        <td>${escapeHtml(status || "-")}</td>
        <td>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">打开</a>` : "-"}</td>
      </tr>`;
    }).join("");
  }

  function renderLogs(logs) {
    $("logBox").innerHTML = (logs || []).slice(-800).map(log => {
      const cls = log.level === "error" ? "error" : log.level === "warn" ? "warn" : "";
      const suffix = log.object_id ? ` object=${log.object_id}` : log.ad_id ? ` ad=${log.ad_id}` : "";
      return `<div class="${cls}">[${escapeHtml(log.ts || "")}] ${escapeHtml(log.level || "info")} ${escapeHtml(log.message || "")}${escapeHtml(suffix)}</div>`;
    }).join("") || "<div>暂无日志</div>";
    $("logBox").scrollTop = $("logBox").scrollHeight;
  }

  function renderJob(job) {
    state.job = job;
    renderStats(job.summary);
    renderDetails(job.items || []);
    renderLogs(job.logs || []);
    const summary = job.summary || {};
    $("currentJob").textContent = `任务 ${job.job_id || ""} / 阶段 ${job.phase || ""} / 状态 ${job.status || ""}`;
    $("deletePostsBtn").disabled = !job.job_id || job.status === "running";
    $("deleteAdsBtn").disabled = !job.job_id || job.status === "running" || !summary.all_posts_deleted;
  }

  async function loadJob(jobId) {
    const job = await api(`/api/fb-post-ad-delete/jobs/${encodeURIComponent(jobId)}`);
    renderJob(job);
    if (job.status === "running") startPolling(jobId);
    else stopPolling();
  }

  function startPolling(jobId) {
    stopPolling();
    state.poll = setInterval(() => loadJob(jobId).catch(error => toast(error.message)), 2500);
  }

  function stopPolling() {
    if (state.poll) clearInterval(state.poll);
    state.poll = null;
  }

  async function preview() {
    const payload = { series_text: $("seriesInput").value, products: $("productsInput").value };
    const job = await api("/api/fb-post-ad-delete/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderJob(job);
    toast("预览完成");
    loadJobs().catch(() => {});
  }

  async function startPhase(phase) {
    if (!state.job) return;
    const confirm = phase === "posts" ? "DELETE_POSTS" : "DELETE_ADS";
    const path = phase === "posts" ? "/api/fb-post-ad-delete/delete-posts" : "/api/fb-post-ad-delete/delete-ads";
    await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: state.job.job_id, confirm }),
    });
    toast("任务已开始");
    startPolling(state.job.job_id);
  }

  async function loadJobs() {
    const data = await api("/api/fb-post-ad-delete/jobs");
    $("jobsBox").innerHTML = (data.items || []).map(job => {
      const s = job.summary || {};
      return `<div class="job-card" data-job="${escapeHtml(job.job_id)}">
        <div><strong>${escapeHtml((job.series_ids || []).join(", ") || job.job_id)}</strong><br><span>${escapeHtml(job.phase)} / ${escapeHtml(job.status)}</span></div>
        <div>Ads ${escapeHtml(s.ads || 0)} / Post ${escapeHtml(s.post_objects || 0)}</div>
      </div>`;
    }).join("") || "<div class=\"empty\">暂无任务</div>";
  }

  async function init() {
    state.auth = await api("/api/ui/topbar").catch(() => ({ authenticated: false }));
    UiTopbar.render({ auth: state.auth, authButton: "topActions" });
    $("topActions").onclick = () => UiTopbar.handleAuthAction({ auth: state.auth, api });
    await QuickNav.render({ container: "#quickNav", activeKey: "fbPostAdDelete" });
    if (!state.auth.authenticated) { $("loginGate").classList.remove("hidden"); return; }
    if (!(state.auth.user && (state.auth.user.is_admin || (state.auth.user.permissions || {}).ad_control_center))) {
      $("permissionGate").classList.remove("hidden");
      return;
    }
    $("pageRoot").classList.remove("hidden");
    renderStats({});
    renderLogs([]);
    $("previewBtn").onclick = () => preview().catch(error => toast(error.message));
    $("deletePostsBtn").onclick = () => startPhase("posts").catch(error => toast(error.message));
    $("deleteAdsBtn").onclick = () => startPhase("ads").catch(error => toast(error.message));
    $("refreshJobsBtn").onclick = () => loadJobs().catch(error => toast(error.message));
    $("jobsBox").onclick = event => {
      const card = event.target.closest("[data-job]");
      if (card) loadJob(card.dataset.job).catch(error => toast(error.message));
    };
    loadJobs().catch(() => {});
  }

  document.addEventListener("DOMContentLoaded", init);
})();
