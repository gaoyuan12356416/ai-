(function (root) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const labels = {running:'执行中', waiting:'等待执行', scheduled:'等待排期', paused:'已暂停', stopped:'已停止', attention_required:'待处理', completed:'已完成', partial_failed:'部分失败', failed:'失败', cancelled:'已取消'};
  const colors = {running:'#175cd3', waiting:'#475467', scheduled:'#475467', paused:'#b54708', stopped:'#667085', attention_required:'#b54708', completed:'#067647', partial_failed:'#b42318', failed:'#b42318', cancelled:'#667085'};
  const segments = run => {
    const c = run.task_counts;
    if (!c) return [`创建时入队 ${run.queued_tasks ?? 0}`, `创建时跳过 ${run.skipped_tasks ?? 0}`];
    const parts = [`已发布 ${c.published}`, `未执行 ${c.pending}`, `执行中 ${c.active}`, `待核对 ${c.review}`, `失败 ${c.failed}`, `跳过 ${c.skipped}`];
    if (c.cancelled) parts.push(`已取消 ${c.cancelled}`);
    if (c.waiting_materials) parts.push(`待素材 Page ${c.waiting_materials}`);
    return parts;
  };
  const api = {
    counts(run) {
      const parts = segments(run);
      const c = run.task_counts;
      const title = c ? `实际子任务 ${c.total}；未执行中：未来排期 ${c.scheduled}，排期早于今天 ${c.overdue}；创建时入队 ${run.queued_tasks}（历史总量）` : '实时统计暂不可用';
      return `<span title="${esc(title)}" style="line-height:1.8;white-space:nowrap">${parts.map(esc).slice(0, 3).join(' / ')}<br>${parts.map(esc).slice(3).join(' / ')}</span>`;
    },
    status(run) {
      const status = run.display_status;
      const label = labels[status] || '待刷新';
      const color = colors[status] || '#667085';
      return `<span class="badge" style="color:${color}">${esc(label)}</span><div style="max-width:230px;margin-top:5px;font-size:12px;line-height:1.5;color:#667085">${esc(run.display_reason || '实时状态暂不可用，请刷新')}</div>`;
    },
    completed(run) {
      return Object.prototype.hasOwnProperty.call(run, 'display_completed_at_utc') ? run.display_completed_at_utc : run.completed_at_utc;
    },
    detail(run) {
      return `当前状态：${labels[run.display_status] || '待刷新'}\n${run.display_reason || ''}\n${segments(run).join(' / ')}\n`;
    }
  };
  root.FBRunSummary = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window === 'undefined' ? globalThis : window);
