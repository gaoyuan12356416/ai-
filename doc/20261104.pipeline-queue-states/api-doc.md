# summary 增量字段

现有 GET /api/pipeline-monitor/summary 保持登录与权限校验。

新增 queue_state_version=1；totals 与每个 channels 项增加 waiting/executing/scheduled/abnormal（整数或 null）。channels 额外提供 abnormal_reasons 原因到数量映射、oldest_pending_seconds。

abnormal_reasons：failed、unknown、held、blocked_by_unknown、worker_offline、repeated_claim、schedule_missing、identity_missing。只返回计数，不能泄漏阻塞任务账号、ID 或凭据。

旧 queued/running/ready/failed/unknown 字段不删，原任务状态筛选不变；接口未知或读取溢出显示 —。成功计数、效率采样、历史暂停沿用原口径。
