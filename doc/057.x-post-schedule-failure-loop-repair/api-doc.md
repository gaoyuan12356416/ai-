# API 文档

## 接口列表

- `POST /internal/posts/schedule-runs/heartbeat`
- `POST /internal/posts/schedule-plan`（兼容新增可选容量证据）
- 本地 CLI：`scripts/x_post_bound_drama_media_recovery.py`

## 请求/响应

### schedule heartbeat

请求沿用冻结身份 `source_type/run_date/publish_time/version/account_ids`，另有布尔 `plan_attempt`。普通续租只更新 `lease_heartbeat_at`；`plan_attempt=true` 同时一次性写 `plan_attempted_at`。响应返回 `heartbeat_recorded` 与 `plan_attempt_recorded`。

### schedule plan capacity proof

`fifo_capacity_skips` 每项必须包含 `pool_item_id/material_id/material_language/reason=language_capacity_full`。Runner 先通过 pool-check 合同持久化当前 run 的素材 ID、来源语言和独立水合时间；OAuth sidecar 根据冻结 run 的完整账号范围重新计算 `material_language_capacities`，store 在同一 SQLite 事务内按 FIFO 顺序核对来源证明新鲜度及“到达该条之前该语言已满”，不能由更旧候选事后凑满。历史 NONBLOCKING/REVALIDATABLE 错误仅保留审计值，不刷新媒体 `last_checked_at`。

### bound drama recovery CLI

输入精确 JSON manifest、DB 路径、部署 commit 和执行人。默认执行 GPU 修复、探测及 store `validate_only`；只有 `--apply` 才 append audit 并 rearm 原队列。输出固定 `x_write_attempted=false`，脚本没有 X 发布调用。

## 错误码

- `x_post_source_query_failed`：只读来源查询连续两次失败。
- `x_post_pool_fifo_conflict`：FIFO 或容量证据不一致。
- `x_post_schedule_plan_attempt_conflict`：同一 claimed run 已跨过计划写入 fence。
- `x_post_schedule_plan_unknown`：计划写入结果无法确认，已停止重复尝试。
- `x_post_schedule_lease_conflict`：心跳身份/状态不一致。
- `x_post_bound_drama_failed_media_recovery_conflict`：恢复清单、绑定、日志或修复证据漂移。

## 兼容性说明

新增请求字段均可选；既有 daily/manual/catchup 路径不变。SQLite 迁移仅新增列、表、索引和不可变审计触发器。旧 runner 不发送容量证据时仍沿用原严格 FIFO。
