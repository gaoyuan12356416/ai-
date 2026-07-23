# SA 测试用例评审

## 结论

有条件通过。P0 已覆盖全局排重、并发/Persistent 幂等、合规筛选、unknown、后台鉴权和 timer 首日门禁；实现后必须补充 migration 副本演练与 production composite 回归证据。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | TC-001/002 | 增量迁移不能只测空库 | 增加真实 007 schema、旧 canary 和重复 legacy fixture | 已采纳 |
| STR-002 | TC-005/019 | timer 重入与首次 Persistent 补跑是独立风险 | 同时测试 DB 唯一约束、run 幂等和 start_date | 已采纳 |
| STR-003 | TC-013/014 | 普通失败与 unknown 不能只看 status | 断言 `attempt_count`、`unknown_outcome` 和 Create Post 调用次数 | 已采纳 |
| STR-004 | TC-016/018 | 日志页面要覆盖服务端和 DOM 泄密 | API DTO 白名单 + HTML escape/URL allowlist + 敏感词扫描 | 已采纳 |
| STR-005 | TC-021 | 部署后不应立即制造额外 Post | 仅验收 timer 状态；首个真实批次由次日自然触发 | 已采纳 |

## QA 修订确认

测试用例已补充上述断言与生产验收边界；自动化实现后逐项回填实际结果。
