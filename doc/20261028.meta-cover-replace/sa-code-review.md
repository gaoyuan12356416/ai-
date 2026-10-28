# 代码评审记录

2026-10-28。范围为新增账本、共享接口、Graph/worker 集成和通知边界；最终发布仍须完整集成复核。

| 级别 | 位置 | 检查项与处理 |
| --- | --- | --- |
| P1 | finish_task | 成功与清理入队合并事务，修复崩溃漏清理窗口 |
| P1 | 双账本写入 | get_cover_delete_once 只读发现共享意图；一次围栏不可绕过 |
| P1 | recover_inflight | 已发送任务转 unknown，保留新 Creative ID 和预期参数 |
| P1 | legacy claim_object | 检查新 reservations/once；不伪造旧任务或覆盖旧锁 |
| P1 | resolve_resource | 仅已知同 ID/账户只读证明可接受，已发绑定依赖绝不重排 |
| P2 | failed 扫描 | 只有 pending/queued/无意图行能重新归批 |
| P2 | 批次通知 | 支持首批积压、晚到回执，忽略纯等待转批和资源计数变化 |
| P2 | payload | 不支持非空字段明确失败，禁止静默损失参数 |

已执行 compileall，通过；完整 cover 快照 66 个通过，随后增量账本 31 个通过；旧 Store 55 个通过。详见 test-report.md。生产 URL 拉取、权限和实际字段响应由自然命中首批确认。
