# SA 评审意见

## 结论

通过，前提是四条根因分别修复，不用给正常素材写伪错误码，也不直接重放未知结果。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | 素材 FIFO | 语言已满缺少可原子验证的跳过证据 | 传精确 pool/material/language，并由 OAuth 重算完整冻结容量 | 已采纳 |
| SA-002 | P0 | 建计划 | 确定性 409 未终态化 | known error 写 failed_preflight；unknown 只读回 | 已采纳 |
| SA-003 | P0 | stale claim | run_date 跨日即停止，与活跃进程竞态 | 独立 heartbeat lease，不能复用 updated_at | 已采纳 |
| SA-004 | P0 | 短剧恢复 | 现有脚本拒绝已绑定历史队列 | 新增精确 manifest + append-only audit + 原队列 rearm | 已采纳 |

## 决策记录

- 语言容量证据只存在于本次计划请求，不污染素材池。
- 租约字段与 FIFO validation cutoff 分离。
- 恢复全部媒体先验证、后单事务应用；发布不属于测试步骤。

## PM 修订确认

已反映到 requirements.md 和 test-cases.md。
