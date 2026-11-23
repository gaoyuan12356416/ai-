/* Derive user-visible production stages from durable run and task receipts. */
(() => {
  function describe(run, now = Date.now() / 1000) {
    const tasks = run.tasks || [];
    const count = (status) => tasks.filter((t) => t.status === status).length;
    const completed = tasks.filter((t) => t.status === "completed" && t.output).length;
    const queued = count("queued"), running = count("running"), failed = count("failed");
    const total = tasks.length;
    let phase = 0, title = "已提交，等待准备", detail = "任务已保存，离开页面后仍会继续处理。", tone = "active";
    if (run.status === "waiting_sources") {
      title = "等待所选素材准备";
      detail = "正在逐集导入和解析，所选素材全部就绪后继续制作。";
    } else if (run.status === "analyzing") {
      title = "正在解析素材";
      detail = "正在识别剧情和关键片段，完成后生成剪辑方案。";
    } else if (run.status === "preparing") {
      phase = 1; title = "正在生成剪辑方案";
      detail = "正在准备剪点、字幕及已选择的配音。";
    } else if (run.status === "rendering" || run.status === "completed" || run.status === "failed") {
      phase = running ? 3 : 2;
      title = running ? "正在制作成片" : "排队等待制作";
      detail = running ? "制作节点已领取任务，正在处理视频；完成后这里会自动更新。" : "正在等待制作节点领取，尚未开始合成视频。无需重复提交。";
      if (total && completed === total) {
        phase = 4; title = "制作完成"; tone = "complete";
        detail = "成片已保存，可直接预览、下载，也可从右上角「我的作品」再次查看。";
      } else if (run.status === "failed" || (failed && !queued && !running)) {
        title = completed ? "部分成片完成，部分失败" : "制作失败"; tone = "error";
        detail = run.error || tasks.find((t) => t.error_message)?.error_message || "请查看失败原因后重试；已完成成片可继续查看和下载。";
      } else if (run.status === "completed") {
        title = "制作已结束，成片待核对"; tone = "error";
        detail = "暂未取得可播放成片，请刷新状态核对。";
      }
    } else if (run.status === "needs_attention") {
      title = "需要处理缺失素材"; tone = "error";
      detail = run.error || "请在下方素材准备区重试失败项，或主动移除后继续。";
    } else if (run.status === "superseded") {
      title = "已按新选择继续"; tone = "muted";
      detail = "此批次已被新的素材选择替代。";
    }
    const oldest = Math.min(...tasks.filter((t) => t.status === "queued").map((t) => t.created_at));
    const waitSeconds = queued && !running && Number.isFinite(oldest) ? Math.max(0, now - oldest) : 0;
    return { phase, title, detail, tone, total, completed, queued, running, failed, waitSeconds };
  }
  const api = { describe };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else window.AIEditorProduction = api;
})();
