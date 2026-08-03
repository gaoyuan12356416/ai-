# SA 测试用例评审

## 结论

`test-cases.md` 保持 87 个计划用例，覆盖方案边界；在真实自动化输出补齐前状态为“待执行”。

## 覆盖映射

| 领域 | 用例 | SA 关注点 |
| --- | --- | --- |
| 独立立即测试 | D01-D23 | `/test-publish`、非成员账号、历史 published、新 job、非终态 key 生命周期、同素材阻断、pool 0 diff |
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

## 不得接受的旧断言

- 不检查 `tt_post_auto_due`、`due_batch/persisted_count` 或不存在的 `tt_post_direct_test_event`；既有 `tt_post_event` 仅做回归；
- 不调用不存在的管理端 direct-test detail/reconcile；
- 不期待 `/run-now` 返回 410；
- 不期待 `processing` 发布状态或 `auto_publish_status=selected_*`；
- 不把 direct-test 目标账号限制为 auto-config 成员。

## 发布门槛

- 87/87 通过，P0/P1 开放缺陷为 0；
- 9 个真实测试脚本退出码 0，migration 在 DB 副本运行两次一致；
- fake GPU/TT publish 证明真实 endpoint 调用为 0；
- 生产只读前后 config/schedule/pool/queue/run/direct-test/ledger/Post 基线一致。
