# BUG-002 `tt_auto_execution_failed · too many SQL variables`

## 已验证根因

2026-08-12 生产任务 146–148 在 selection 阶段失败。三条任务均没有 `material_id`、
`gpu_job_id`、`publish_id` 或未知发布结果，事件只包含
`task_claimed_selection -> task_transient_failure`。

冻结模板 2 v1 的 `drama_launch_window_days=0` 会读取全部英文已发布短剧：当次只读复现为
22,227 行、11,119 个 distinct `content_id`。指标缓存查询同时绑定 1 个 platform、7 个
metric date、1 个 product 和 11,119 个 content ID，共 11,128 个 SQLite 变量。生产
SQLite 3.26.0 没有提高 `MAX_VARIABLE_NUMBER`，因此超过默认 999 上限并抛出
`too many SQL variables`。

模板 2 v2 已将窗口收窄为 60 天：同一只读复现为 1,346 行、673 个 distinct
`content_id`，总绑定变量 682，低于生产上限。任务 149–151 仍 pending 是因为同账号的
146–148 处于 `retry_wait`，账号串行门禁不允许后续任务越过前序活动任务。

## 本次范围

本次仅增加可审计的安全强制关闭能力，不修改指标缓存查询。若需要永久支持
`drama_launch_window_days=0`，应单独将 `iter_ready_metric_rows` 的 content ID 条件改为
分批查询，并补充跨批次去重、稳定排序与大候选集回归测试。
