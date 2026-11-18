/* Administrator-only service drain detail, refreshed independently of claims. */
(() => {
  "use strict";
  const labels = { "pipeline-coordinator.service": "任务调度器", "drama-material-api.service": "主 API", "x-post-automation.service": "X 发布服务", "x-tag-post.service": "X 标签发布服务" };
  const reasons = { execution_in_progress: "进程仍有心跳，等待任务完成", execution_still_active: "选材请求仍在执行", selection_recovery_check_pending: "请求已结束，等待恢复检查", reservation_or_attempt_requires_review: "已有素材或执行记录，需要核对", heartbeat_stale_requires_review: "心跳已过期，需要核对进程" };
  const phases = { selection: "选材", selecting: "选材", prepare: "制作", preparing: "制作", publish: "发布", publishing: "发布", reconcile: "核对回执", render: "渲染", cos_render: "云转码", download: "下载", upload: "上传" };
  let panel;
  function text(tag, value) { const el = document.createElement(tag); el.textContent = value; return el; }
  function time(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "暂无记录"; }
  async function refresh() {
    try {
      const response = await fetch("/api/pipeline-monitor/drain-diagnostics", { credentials: "same-origin", cache: "no-store" });
      if (response.status === 401 || response.status === 403) return;
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      const anchor = document.getElementById("pmWarnings");
      if (!anchor) return;
      if (!panel) { panel = text("section", ""); panel.className = "pm-panel"; panel.id = "pmDrainDiagnostics"; anchor.after(panel); }
      const expanded = new Set(Array.from(panel.querySelectorAll("details[open]")).map(el => el.dataset.unit));
      panel.replaceChildren();
      panel.hidden = !(data.holds || []).length && !data.stale && !data.selection_recovery?.error;
      if (data.stale) panel.append(text("p", "排空诊断更新延迟，当前信息可能已变化。"));
      if (data.selection_recovery?.error) panel.append(text("p", "选材恢复检查暂未完成，任务保留待核对。"));
      for (const hold of data.holds || []) {
        panel.append(text("h3", `${labels[hold.unit] || hold.unit}排空已持续 ${Math.floor((hold.elapsed_seconds || 0) / 60)} 分钟 · 剩余 ${hold.pending_count} 条${hold.overdue ? " · 已超过 10 分钟，请关注阻塞任务" : ""}`));
        const details = document.createElement("details");
        details.dataset.unit = hold.unit; details.open = expanded.has(hold.unit);
        details.append(text("summary", "查看阻塞任务、阶段和最近进度"));
        const scroll = document.createElement("div"); scroll.className = "pm-table-scroll";
        const table = document.createElement("table"); table.className = "pm-table";
        const header = document.createElement("tr");
        ["任务", "阶段", "最近进度", "最近心跳", "等待原因"].forEach(v => header.append(text("th", v)));
        table.append(header);
        for (const job of hold.blockers || []) {
          const row = document.createElement("tr");
          [job.source_key || job.job_id, phases[job.phase] || job.phase, time(job.last_progress_at), time(job.last_heartbeat_at), reasons[job.reason] || job.reason].forEach(v => row.append(text("td", String(v || "—"))));
          table.append(row);
        }
        scroll.append(table); details.append(scroll); panel.append(details);
      }
    } catch (_) {
      if (panel && !panel.hidden) panel.replaceChildren(text("p", "排空诊断暂不可用，正在重新获取。"));
    } finally { window.setTimeout(refresh, 15000); }
  }
  refresh();
})();
