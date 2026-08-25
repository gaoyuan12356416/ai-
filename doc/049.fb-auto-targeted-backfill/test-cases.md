# 测试用例

## 测试范围

`create_run` 白名单/版本锁、来源运行校验、指纹、防重复、报告路径、真实建单范围及普通自动发布回归。

## 测试数据

临时 SQLite、假 Page Repository、假素材快照；生产仅用 run 20 和固定五个 Page 做 dry-run，不通过测试创建真实 Graph Post。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 白名单建单 | 模板启用，2 可用+1缺 Token+1非目标 | 指定 3 个目标调用 `create_run` | 仅 3 个快照/任务，2 planned、1 skipped | P0 | 待执行 |
| TC-002 | 目标缺失 | 第二次 Page 查询移除一个目标 | 调用建单 | 原子拒绝，无 run/task | P0 | 待执行 |
| TC-003 | 模板版本变化/停用 | 白名单校验期间更新模板 | 调用建单 | 事务前后拒绝，无任务 | P0 | 待执行 |
| TC-004 | 不合法组合 | auto 或无版本锁使用白名单 | 调用建单 | `invalid_request` | P0 | 待执行 |
| TC-005 | 来源任务不满足 | 非 skipped、已有 attempt/ledger/unknown | validate | 拒绝且无写入 | P0 | 待执行 |
| TC-006 | 目标集合不精确 | 少/多一个 Page | validate | 拒绝且无写入 | P0 | 待执行 |
| TC-007 | dry-run | 来源/目标合法 | validate-only | 输出 5 个目标和指纹，run 数不变 | P0 | 待执行 |
| TC-008 | 指纹漂移 | dry-run 后 Token 计数或模板变化 | apply 旧指纹 | 拒绝且无建单 | P0 | 待执行 |
| TC-009 | 正确 apply | 指纹一致 | apply | 只建一个 5-Page 手动 run | P0 | 待执行 |
| TC-010 | 同操作幂等 | TC-009 已完成 | 重复 apply | 返回相同 run_id，不增任务 | P0 | 待执行 |
| TC-011 | 不同操作重复回补 | 已存在同来源恢复 run | 换 operation id | 拒绝 | P0 | 待执行 |
| TC-012 | 报告路径越界/覆盖/软链接 | 非专用目录或已存在 | apply | 建单前拒绝 | P0 | 待执行 |
| TC-013 | 普通调度回归 | 原有测试夹具 | 跑 FB 全量测试 | 全部通过、原参数行为不变 | P1 | 待执行 |
| TC-014 | 生产终态 | dry-run/apply 已审计 | 观察 prepare/execute/reconcile | 4个成功或明确失败；1个缺Token跳过；无 unknown | P0 | 待执行 |

## 回归范围

`scripts/test_fb_auto*.py` 全集；重点覆盖自动 due-slot、容量、素材冷却、预制、发布凭证切换、unknown/reconcile、服务/API 契约和部署检查。
