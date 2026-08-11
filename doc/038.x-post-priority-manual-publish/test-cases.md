# 测试用例

## 测试范围

覆盖高优 schema/排序/并发、手动批次幂等/预检/原子队列/恢复/聚合、内部与公开鉴权、UI 和既有 X 调度/账本回归。

## 测试数据

仅使用临时 SQLite、mock 账号、mock MySQL、临时媒体和伪造 X client。所有测试禁止访问真实 X 写接口。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | additive migration | V3 生产结构副本 | 连续执行两次 `ensure_storage()` | 高优/manual 字段表索引存在，历史行/queue/log 不变 | P0 | 生产副本通过 |
| TC-002 | 未分配短剧高优 | pending、无错误、未分配 | 设置高优 | `created_at` 不变，记录优先时间和操作人 | P0 | 本地通过 |
| TC-003 | 取消高优 | 已高优未分配短剧 | 取消 | 高优字段清空，恢复普通排序 | P1 | 本地通过 |
| TC-004 | 多高优顺序 | 两部未分配短剧 | 依次高优 | 最近高优在前，其后按高优时间倒序 | P0 | 本地通过 |
| TC-005 | 已绑定短剧保护 | active 且已绑定 | 设置高优 | 409，绑定和进度不变 | P0 | 本地通过 |
| TC-006 | 高优与调度竞争 | 页面读取后短剧被分配 | 提交高优 | 事务复查返回 409，不改记录 | P0 | 本地通过 |
| TC-007 | 已绑定优先续发 | 一账号已有绑定且池内有高优新剧 | 生成候选 | 原绑定剧继续，空闲账号才选择高优剧 | P0 | 本地通过 |
| TC-008 | 冻结计划不变 | 已有 schedule queues | 高优另一短剧 | 已有 queues 不变，下个未冻结点使用新顺序 | P0 | 本地通过 |
| TC-009 | 手动请求正常创建 | N 个唯一 ID/N 个合格账号 | POST 请求 | 202，同一 manual run，素材池不新增 | P0 | 本地通过 |
| TC-010 | 数量不等 | 2 素材/3账号 | POST | 400，无 run/queue | P0 | 本地通过 |
| TC-011 | 输入重复 | 重复素材或账号 | POST | 400，无 run/queue | P0 | 本地通过 |
| TC-012 | 客户端幂等重放 | 同幂等键同 payload | 连续 POST | 返回相同 run ID，只存在一行 | P0 | 本地通过 |
| TC-013 | 幂等冲突 | 同键不同 payload | POST | 409，不覆盖冻结输入 | P0 | 本地通过 |
| TC-014 | 素材已在池中 | pool 存在同 key | worker 建计划 | failed_preflight 或 409，queue=0 | P0 | 本地通过 |
| TC-015 | 素材已有历史 | queue 存在同 key | worker 建计划 | 整批失败，历史不改 | P0 | 本地通过 |
| TC-016 | selector 任一拒绝 | 一条违规/映射缺失 | 执行 worker | run=failed_preflight，queue/log=0 | P0 | 本地通过 |
| TC-017 | 媒体任一拒绝 | 一条下载/探测失败 | 执行 worker | run=failed_preflight，queue/log=0 | P0 | 本地通过 |
| TC-018 | 长视频账号匹配 | 普通+Premium账号，短+长素材 | 预检 | 长视频只配 Premium，账号集合不变 | P0 | 本地通过 |
| TC-019 | 无合格会员 | 长视频且所有账号 none | 预检 | failed_preflight，不写 X | P0 | 本地通过 |
| TC-020 | 原子建队列 | 全批预检通过 | create plan | N 个 queue 一次提交，均关联 manual_run_id | P0 | 本地通过 |
| TC-021 | 建队列并发冲突 | 事务前另一队列占用其中一素材 | create plan | 全批回滚，新增 queue=0 | P0 | 本地通过 |
| TC-022 | 手动与日更同日 | 账号当日已有 schedule queue | create manual plan | 允许显式手动 queue；manual 内账号仍唯一 | P0 | 本地通过 |
| TC-023 | worker 无队列恢复 | run=running、queue=0 | 重启 worker | 可重做预检，只创建一组 queue | P0 | 本地通过 |
| TC-024 | worker 有队列恢复 | 部分发布后进程停止 | 重启 worker | 只读取冻结 queues，不重新选材/建队列 | P0 | 本地通过 |
| TC-025 | 全部成功聚合 | N 个明确成功 | 串行处理 | run=completed，计数准确 | P0 | 本地通过 |
| TC-026 | known failure 继续 | 中间账号明确失败 | 串行处理 | 后续账号仍尝试，最终 completed_with_errors | P0 | 本地通过 |
| TC-027 | 限流停批 | 中间账号 rate limit | 串行处理 | run=stopped，后续 queued 未尝试 | P0 | 本地通过 |
| TC-028 | unknown 停批 | 中间响应未知 | 串行处理 | run=needs_review，后续不调用 X | P0 | 本地通过 |
| TC-029 | 队列后永久去重 | manual queue 已明确失败 | 再入池/再手动 | 均拒绝，不删除绑定 | P0 | 本地通过 |
| TC-030 | 权限与 CSRF | anonymous/API token/cross-origin | 调管理写接口 | 401/403；Cookie+导航+同源才允许 | P0 | 本地通过 |
| TC-031 | daily token 最小权限 | daily bearer | 调 create/query admin 接口 | 只可领取/处理 backend 已创建 run，不可创建用户请求 | P0 | 本地通过 |
| TC-032 | 页面高优交互 | 可用/不可用短剧混合 | 渲染并点击 | 仅可用未分配项可操作，有徽标/取消入口 | P1 | 本地通过 |
| TC-033 | 手动弹窗 | 已保存5账号、5素材 | 点击手动按钮 | 默认5账号、警告/数量/确认正确，不保存排期 | P0 | 本地通过 |
| TC-034 | 页面幂等与轮询 | 提交响应丢失/刷新 | 重试/轮询 | 复用同键同 run，展示终态和日志入口 | P0 | 本地通过 |
| TC-035 | X 既有回归 | 历史 daily/catchup/schedule/canary | 跑既有测试 | 选择、绑定、排期、文案、长视频、去重无回归 | P0 | 本地通过 |
| TC-036 | 无真实发布部署验收 | 生产备份已完成 | 迁移/服务/HTTP/DB检查 | queue/log/post 数量不增加，timer 和服务正常 | P0 | 生产通过 |

## 回归范围

- `scripts/test_x_post_daily.py`
- `scripts/test_x_post_schedule_runner.py`
- `scripts/test_x_post_multi_schedule_store.py`
- `scripts/test_x_post_drama_selector.py`
- `scripts/test_x_post_material_pool.py`
- `scripts/test_x_post_ledger.py`
- X 素材池/短剧池页面 DOM 合同和所有授权边界。
