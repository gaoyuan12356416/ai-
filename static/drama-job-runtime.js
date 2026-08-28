(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DramaJobRuntime = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const ACTIVE = new Set(["queued", "validating", "downloading", "processing_cover", "rendering", "removing_bgm"]);
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }
  function date(value) {
    if (!value) return null;
    const text = String(value).trim();
    // Database timestamps are UTC; explicit ISO offsets retain their meaning.
    const normalized = /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(text) ? text.replace(" ", "T") + "Z" : text;
    const result = new Date(normalized);
    return Number.isFinite(result.getTime()) ? result : null;
  }
  function secondsText(total) {
    if (!Number.isFinite(total) || total < 0) return "--";
    total = Math.floor(total);
    const h = Math.floor(total / 3600), m = Math.floor(total / 60) % 60, s = total % 60;
    return h ? `${h}小时${m}分${s}秒` : m ? `${m}分${s}秒` : `${s}秒`;
  }
  function elapsed(job, now = Date.now()) {
    const rt = job.remote_runtime || {};
    const start = date(rt.first_started_at || rt.started_at || job.active_started_at || job.started_at || job.created_at);
    if (!start) return "--";
    const terminal = job.status === "done" || (job.status === "failed" && !["queued", "running"].includes(rt.status));
    const end = terminal ? date(job.active_finished_at || job.finished_at || rt.completed_at || rt.finished_at || rt.updated_at || job.updated_at) : null;
    if (terminal && !end) return "--";
    return secondsText(((end ? end.getTime() : now) - start.getTime()) / 1000);
  }
  function progressHtml(job) {
    const view = job.remote_progress;
    if (!view) {
      const p = Math.max(0, Math.min(100, Number(job.progress) || 0));
      return `<div>${p}%</div><div class="progress"><span style="width:${p}%"></span></div><div class="muted" style="margin-top:6px;max-width:230px;">${esc(job.progress_detail || "--")}</div>`;
    }
    const raw = view.stage_percent;
    const known = raw !== null && raw !== undefined && Number.isFinite(Number(raw));
    const p = known ? Math.max(0, Math.min(100, Number(raw))) : null;
    const bar = known ? `<div class="progress"><span style="width:${p}%"></span></div>` : "";
    const rt = job.remote_runtime || {};
    const stamp = date(rt.last_progress_at);
    const last = stamp ? `<div class="muted">最后进展 ${esc(stamp.toLocaleTimeString("zh-CN", {hour12:false,timeZone:"Asia/Shanghai"}))}</div>` : "";
    const cover = job.cover_16x9_url ? '<div class="muted">封面已完成</div>' : "";
    return `<div>${esc(view.stage_label || "制作中")}${known ? ` · 本阶段 ${p}%` : ""}</div>${bar}<div class="muted" style="margin-top:6px;max-width:230px;">${esc(view.detail || "")}</div>${cover}${last}`;
  }
  function install({getJobs, refresh, canRead = () => true, document: doc = globalThis.document}) {
    let refreshing = false, lastFetch = 0;
    const timer = setInterval(async () => {
      if (!doc || doc.visibilityState === "hidden" || !canRead()) return;
      const jobs = getJobs() || [];
      const byId = new Map(jobs.map(j => [j.job_id, j]));
      doc.querySelectorAll("[data-drama-elapsed]").forEach(el => {
        const job = byId.get(el.getAttribute("data-drama-elapsed"));
        if (job) el.textContent = elapsed(job);
      });
      const active = jobs.some(j => ACTIVE.has(j.status) || (j.remote_runtime && ["queued", "running"].includes(j.remote_runtime.status)));
      if (!active || refreshing || Date.now() - lastFetch < 10000) return;
      refreshing = true;
      lastFetch = Date.now();
      try { await refresh(); } catch (_) { /* Preserve the last visible state on transport errors. */ }
      finally { refreshing = false; }
    }, 1000);
    return () => clearInterval(timer);
  }
  return {elapsed, progressHtml, install, date, secondsText};
});
