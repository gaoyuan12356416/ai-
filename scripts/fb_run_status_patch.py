"""Exact fragments preserve independent live Page scheduling UI changes."""
HTML_REPLACEMENTS = (
    ('<th>Page统计</th><th>任务</th>', '<th>Page统计（创建时）</th><th>任务（实时）</th>'),
    ('<script>(()=>', '<script src="/fb-auto-publish-run-summary.js?v=20261124v1"></script><script>(()=>'),
    ('formatTime(r.completed_at_utc)', 'formatTime(FBRunSummary.completed(r))'),
    ('<td>排队${r.queued_tasks} / 跳过${r.skipped_tasks}</td>', '<td>${FBRunSummary.counts(r)}</td>'),
    ('<span class="badge">${esc(r.status)}</span>', '${FBRunSummary.status(r)}'),
    ('formatTime(d.run.completed_at_utc)', 'formatTime(FBRunSummary.completed(d.run))'),
    ('原始运行数据（*_utc 字段为 UTC）：', '${FBRunSummary.detail(d.run)}\\n运行明细（status 为原始记录，display_status 为实时展示状态；*_utc 字段为 UTC）：'),
    ('“完成时间”表示本批次结束，发布结果以详情中的对账状态为准。', '任务数量和状态按当前明细统计；“未执行”包含待制作及待发布，“待处理”包含逾期、结果未知或执行状态待核对。“完成时间”仅在全部任务结束后展示。'),
)


def patch_html(text):
    for before, after in HTML_REPLACEMENTS:
        if text.count(before) != 1:
            raise RuntimeError('html_anchor_changed:' + before[:70])
        text = text.replace(before, after, 1)
    return text
