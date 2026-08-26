# 057.x-post-schedule-failure-loop-repair 需求与技术设计

## 背景

2026-08-25 至 2026-08-26 的 X 定时发布出现四类失败：素材全局 FIFO 与语言容量规则冲突并持续重试；只读候选查询异常信息不可定位且无有限重试；跨午夜仍在执行的批次被误判 stale；短剧历史 pre-X 媒体尺寸失败占满所有可用绑定。线上 Run 345 在零队列、零日志、零 X 写入状态下每 11–12 分钟固定重现。

## 目标

- 确定性计划创建失败必须进入终态，不能继续 claimed 循环。
- 保持全局 FIFO，同时允许已达到该语言冻结账号容量的干净素材留给后续批次。
- 只读候选查询仅做一次重连重试，并输出安全、可操作的阶段错误。
- 用执行租约消除跨午夜 stale 竞态。
- 为已绑定短剧的 pre-X 媒体失败提供清单锁定、全量验证、append-only 审计和原队列 rearm。
- 全部测试不创建真实 X Post。

## 范围

### 包含

- X schedule runner、SQLite ledger、OAuth sidecar 内部接口及离线回归。
- Run 274 的 14 条短剧失败媒体修复工具和清单。
- 生产备份、GitHub-first 发布、回滚和自然定时验收。

### 不包含

- X Token、账号范围、发布时间及文案规则变更。
- 改写已发布历史、换绑账号/短剧、重复创建队列。
- 用真实 X Post 作为测试手段。

## 用户故事 / 业务规则

1. 运营看到失败原因时应得到中文可执行结论，而不是裸 `OperationalError`。
2. 一种语言账号已选满时，前序同语言素材必须保持 unpublished 且无伪错误；服务端只接受精确、本批次、容量已满的跳过证据。
3. 原子建计划返回确定性错误时，冻结 run 必须记录 `failed_preflight`；响应未知时只能读回 ledger，不能重新选材或再次写计划。
4. 只读查询第一页失败后可重连一次；第二次失败必须零队列、零发布并终态化。
5. 活跃 planner 每个候选前续租；跨日只停止超过 2 小时且无续租的批次。
6. 短剧恢复必须锁定 run、queue、pool、content、episode、account、错误码及零 attempt/unknown，全部媒体成功后才单事务 rearm。

## 交互与流程

`due claim -> heartbeat -> frozen pool read -> bounded MySQL retry -> media preflight -> atomic plan -> frozen queue publish`。任何失败都先查 ledger；只有明确不存在队列时才记录 preflight 失败。

## 技术设计

### 影响模块

- `scripts/x_post_schedule_runner.py`
- `features/x_posts/selector.py`
- `features/x_posts/service.py`
- `features/x_accounts/oauth_service.py`
- 短剧失败媒体恢复脚本及离线测试

### 数据结构

- `x_post_schedule_run.lease_heartbeat_at`：与 `updated_at` 分离，避免破坏 FIFO 本轮校验 cutoff。
- 素材容量跳过不落素材池错误；当前 run 先持久化素材 ID、来源语言及水合时间，计划事务再按 FIFO 到达顺序、冻结语言容量和新鲜度原子消费。
- 短剧恢复新增 append-only audit，记录修复前后 URL、SHA、大小、时长、job key、绑定身份和部署提交。

### API / 接口

- `POST /internal/posts/schedule-runs/heartbeat`：仅 loopback internal bearer，精确冻结身份续租。
- `POST /internal/posts/schedule-plan` 新增可选 `fifo_capacity_skips`，OAuth 层按完整冻结账号重新计算语言容量。

### 异常与边界

- 历史 revalidatable 错误只有 `last_checked_at >= claimed cutoff` 才可作为跳过证据。
- 容量证明使用独立 `source_hydrated_at`，保留 NONBLOCKING/REVALIDATABLE 历史错误且不刷新 `last_checked_at`，避免把未做的媒体复验伪装成本轮证据。
- 任意容量证据 ID/素材/来源语言/水合时间/数量不一致，或该 FIFO 位置之前尚未满额，均返回 `x_post_pool_fifo_conflict` 且零队列。
- plan response unknown 且读回失败时停止，不发布、不写失败覆盖未知结果。
- 心跳身份、版本或状态不一致返回租约冲突。

## 验收标准

- Run 345 精确 19 条回放：无容量证据失败；携带 pool 820 精确 ja 容量证据通过；pool 820 保持干净。
- 确定性 FIFO 409 只调用一次 create，随后 `failed_preflight`，无 publish。
- 候选查询首次失败、第二次成功可继续；连续两次失败仅一个终态失败记录，零 create/publish。
- 23:44 活跃批次跨午夜仍 claimed；租约超过 2 小时才 stale。
- Run 274 的 14 条恢复 validate-only 零写；应用后原队列 rearm、无新队列、无绑定漂移；自然发布前不调用 X。
- SQLite `quick_check=ok`、外键异常为 0；相关完整测试通过。

## 风险与待确认

- Run 274 的恢复应用会生成修复媒体并重启其冻结队列；实际 X 发布只由恢复后的 frozen backlog 自然执行，不作为测试。
- 发布前需再次只读确认 Run 345/274 状态与 manifest 无漂移。

## 变更记录

- 2026-08-26：建立需求、SA、QA、部署与回滚证据链。
