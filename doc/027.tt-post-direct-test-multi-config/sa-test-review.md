# SA 测试用例评审

## 结论

027 基线 87 个用例已有历史通过证据。BUG-005 新增 T01-T12 后总计划数为 99；新增 12 个用例已由 service/UI/app 契约测试覆盖并通过，9 个 Python 脚本共 `341/341`、Node bridge `53/53`。本地门禁通过，待生产只读 0 副作用验收。

## 覆盖映射

| 领域 | 用例 | SA 关注点 |
| --- | --- | --- |
| 独立立即测试 | D01-D23 | `/test-publish`、非成员账号、历史 published、新 job、非终态 key 生命周期、同素材阻断、pool 0 diff |
| 统一发布任务主表 | T01-T12 | 服务端合并、类型/ID 隔离、分页前统计、组合筛选、稳定分页、queue 事件兼容、direct 只读、0 发布副作用 |
| 发布投影/自动互斥 | P01-P12 | 三态扁平字段、计数、consumed 不误判、active/unknown 临时阻断、direct 终态后 auto 可领取 |
| 原子配置/UI | C01-C22 | 一个版本保存三部分、关闭 consent 特例、准确成员状态、单素材入池合同 |
| 同分钟 | S01-S10 | 全部 slot 先 claim、逐项原子、无素材不建 run、limit 仅限执行、崩溃恢复 |
| 迁移/回滚 | M01-M10 | 两张表、幂等初始化、mixed 两步迁移、回滚保留当前 DB |
| 安全/无副作用 | N01-N10 | 权限脱敏、fake 上游、只读生产、旧 `/run-now` 兼容但 UI 不调用 |

## 必须观察的时序证据

1. 在 fake creator-info 的第一次调用点读取 DB：所有“有可用素材”的同分钟 slot 已有 `tt_post_schedule_run(status=claimed)` 和对应 pool reservation；即使存在旧 claimed/unbound recovery 也成立。
2. `limit=1` 时上述 preclaim 数不受限，只有执行/返回 item 数受限。
3. 第 N 个 claim 注入失败时，仅该独立事务不写；后续 slot 仍尝试，且首个 creator-info 仍晚于整个 preclaim 循环。
4. 无素材 slot 返回 skipped，不要求伪造 run 行。

## BUG-005 必须观察的只读证据

1. 在相同临时 DB 中保存增量前 `/queue` 响应快照；实现后 `/queue` items/summary/pagination/排序逐字段一致。
2. `/tasks` 使用跨表相同数字 ID fixture，必须同时返回 `automatic:1` 与 `direct_test:1`；只有 automatic 行可生成 event/cancel/reconcile 请求。
3. summary 必须在过滤后、分页前计算。以 `page_size=2` 查询多条混合任务，统计仍等于完整过滤集合，而不是当前页。
4. 数据不变时遍历全部分页两次，任务 key 顺序一致、集合无重复无遗漏；非法 type/page 被拒绝。
5. 多次加载、筛选和翻页前后比较 SQLite 行、hash、updated_at、GPU ledger 与 fake publish 调用计数，全部 0 diff/0 调用。

## 不得接受的旧断言

- 不检查 `tt_post_auto_due`、`due_batch/persisted_count` 或不存在的 `tt_post_direct_test_event`；既有 `tt_post_event` 仅做回归；
- 不调用不存在的管理端 direct-test detail/reconcile；
- 不期待 `/run-now` 返回 410；
- 不期待 `processing` 发布状态或 `auto_publish_status=selected_*`；
- 不把 direct-test 目标账号限制为 auto-config 成员。
- 不在浏览器拼接 `/queue` 与 `/direct-tests` 的已分页结果；合并、统计和分页必须由服务端只读投影完成。
- 不为 direct-test 伪造 `tt_post_event`，也不把 `direct_test_id` 传入任何 queue action。
- 不因新增 `/tasks` 改写或废弃旧 `/queue`。

## 发布门槛

- 99/99 通过，P0/P1 开放缺陷为 0；其中 T01-T12 必须有新增自动化证据；
- 9 个真实测试脚本退出码 0，migration 在 DB 副本运行两次一致；
- fake GPU/TT publish 证明真实 endpoint 调用为 0；
- 生产只读前后 config/schedule/pool/queue/run/direct-test/ledger/Post 基线一致；Network 只新增 GET `/tasks`，不得出现发布写请求。
