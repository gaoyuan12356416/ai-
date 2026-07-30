# 测试用例

## 测试范围

覆盖快速校验、queued 入池、幂等与去重、预制作状态机、账号 FIFO、租约/fencing、重试、原子转 ready、页面交互、systemd 契约与既有发布流程回归。

## 测试数据

- 临时 SQLite 数据库，不读取或改写生产账本。
- Fake 素材解析器：合法素材、缺失素材、Drama ID 不一致。
- Fake GPU：立即成功、长执行、5xx、终态元数据错误、超时、返回 job/content/profile 不一致。
- 两个账号 A/B，每个账号两条素材，用于 FIFO 与跨账号验证。
- 所有 publish gates 关闭；禁止真实 TikTok API 写操作。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 快速校验合法素材 | resolver 返回素材 | 调用 preview | 返回 `validated/not_started/publish_ready=false`；GPU 调用为 0 | P0 | 待执行 |
| TC-002 | 快速校验不存在素材 | resolver 返回 not found | 调用 preview | 返回明确 4xx；不建 intake；GPU 调用为 0 | P0 | 待执行 |
| TC-003 | `/materials/prepare` 兼容 | 同 TC-001 | 调用兼容入口 | 与 preview 相同，只校验不制作 | P1 | 待执行 |
| TC-004 | 校验后立即入池 | 账号/设置/素材合法 | POST material-pool | 返回 `queued`、`publish_ready=false`；请求不等 GPU | P0 | 待执行 |
| TC-005 | 入池 Drama ID 防篡改 | 提交与 resolver 不同的 content ID | POST material-pool | `409 tt_content_id_mismatch`，不入池 | P0 | 待执行 |
| TC-006 | 同键同请求重放 | 已有 queued intake | 原样再次 POST | 返回同一 intake，不新增行、不改变冻结值 | P0 | 待执行 |
| TC-007 | 同键不同请求冲突 | 同键但账号/文案/素材不同 | 再次 POST | `409 ...idempotency_conflict` | P0 | 待执行 |
| TC-008 | 同素材冻结信息冲突 | 同 material ID，不同请求摘要 | 再次 POST | `409 ...intake_conflict` | P0 | 待执行 |
| TC-009 | 跨历史池去重 | material 已在 ready/旧池/queue | 尝试入池 | `409 tt_post_material_already_used` | P0 | 待执行 |
| TC-010 | kick 失败兜底 | kick 文件不可写 | 入池 | 入池仍成功，返回 wakeup=false；timer 后可领取 | P1 | 待执行 |
| TC-011 | 单账号严格 FIFO | A1、A2 queued | 连续 claim | 先 A1；A1 活动时不得领取 A2 | P0 | 待执行 |
| TC-012 | 跨账号队首 | A1/A2、B1/B2 | claim 并保持 A1 活动 | 可领取 B1，不得领取 A2/B2 | P0 | 待执行 |
| TC-013 | 正常续租 | claim A1 | 用正确 token renew | lease 延长，状态仍 preparing | P0 | 待执行 |
| TC-014 | 错 token/过期 token | claim 后伪造 token或推进时间 | renew/process | `409 claim_invalid` | P0 | 待执行 |
| TC-015 | 过期租约恢复 | worker-1 claim 后崩溃 | 过期后 worker-2 claim | 获得新 token；worker-1 不能完成 | P0 | 待执行 |
| TC-016 | 正常预制作完成 | GPU 返回合法结果 | process | intake ready；同事务新增 available ready pool；结果字段一致 | P0 | 待执行 |
| TC-017 | 完成幂等 | 已用同结果完成 | 重放同完成 | 返回原 ready；不同成片结果冲突 | P0 | 待执行 |
| TC-018 | 完成时素材被占用 | prepare 期间 material 进入其他历史表 | complete | 失败关闭，不覆盖/重复发布 | P0 | 待执行 |
| TC-019 | 临时错误重试 | GPU/sidecar 5xx，attempt<5 | process | `retry_wait`，next_attempt 在未来；到时可再 claim | P0 | 待执行 |
| TC-020 | 达最大次数 | 第 5 次仍临时失败 | process | `failed`，不再自动 claim | P0 | 待执行 |
| TC-021 | 终态错误 | 元数据/profile/job/content/时长不合法 | process | 直接 `failed`，不进入 retry_wait | P0 | 待执行 |
| TC-022 | 实时账号限制 | 校验时合法，制作时账号限制变化 | process | 重新读取 Creator Info 并 failed，不写 ready pool | P0 | 待执行 |
| TC-023 | 非 ready 不可发布 | 各建 queued/preparing/retry_wait/failed | due/run-now | 都不被消费，available count 不包含这些行 | P0 | 待执行 |
| TC-024 | ready 可发布计数 | 完成一条 intake | GET pool/schedule | `ready/publish_ready=true`，available count +1 | P0 | 待执行 |
| TC-025 | 合并素材池列表 | 同时有 intake、关联 ready 与历史 ready | GET material-pool | 无重复，状态/汇总/分页/筛选正确 | P1 | 待执行 |
| TC-026 | 公共响应脱敏 | 有 preparing claim | GET pool/API error | 不返回 claim token、lease、bearer 或完整敏感信息 | P0 | 待执行 |
| TC-027 | runner 空闲 | 无候选 | 执行 runner tick | `idle`，不 renew/process | P1 | 待执行 |
| TC-028 | runner 单条合同 | 多条 queued | 执行一次 tick | 最多 claim/process 1 条 | P0 | 待执行 |
| TC-029 | runner 心跳 | process 长于 renew interval | 执行 tick | 周期续租；结束后线程停止 | P0 | 待执行 |
| TC-030 | runner 本机边界 | internal URL 非 loopback/端口不对 | 启动 runner | 配置校验失败，不发请求 | P0 | 待执行 |
| TC-031 | 超时关系校验 | process timeout < gpu+60 或 lease < 3×renew | 启动 runner | 配置校验失败 | P0 | 待执行 |
| TC-032 | 进程锁 | 两个 runner 同时启动 | 执行 tick | 一个处理，另一个 `skipped_locked` | P1 | 待执行 |
| TC-033 | UI 校验不等待制作 | 页面填一条合法素材 | 点击批量校验 | 快速显示 Drama ID，无视频预览/成片等待 | P0 | 待执行 |
| TC-034 | UI 入池后恢复 | 校验通过并提交 | 观察按钮和状态表 | 立即提示后台预制作；表单解除 busy；显示 queued | P0 | 待执行 |
| TC-035 | UI 状态轮询 | queued 逐步变 ready | 等待轮询/刷新页面 | 显示状态迁移，页面关闭不影响任务 | P1 | 待执行 |
| TC-036 | 发布 runner 回归 | 既有 ready pool 和 schedule | 跑原测试 | due、manual、claim、publish、reconcile 行为不变 | P0 | 待执行 |
| TC-037 | 无真实发布 canary | 生产 gates 全关闭 | 校验、入池、等待 ready | 无 TikTok publish ID/帖子；仅制作链路产生记录 | P0 | 待执行 |

## 回归范围

- TT 账号读取、账号设置与 Creator Info。
- 每日发布时点保存、立即发布幂等、账号串行发布。
- recurring pool 的 claim/freeze/bind/recovery。
- GPU prepare 合同、最终媒体校验与 COS 地址。
- 主 API 的 TT 路由代理、静态页语法和安全渲染。
- 三个 live gates 默认关闭及日志脱敏。
