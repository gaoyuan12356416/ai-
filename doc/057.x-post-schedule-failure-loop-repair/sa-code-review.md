# SA 代码评审

## 结论

通过。独立审查最终未发现剩余 P0/P1；可以进入 GitHub-first 发布门禁。

## 评审范围

Selector 脱敏查询、schedule runner known/unknown outcome、Store FIFO/容量证明、lease/stale、OAuth schedule DTO、Run 274 bound-drama 恢复、relay 与 mixed published/failed 状态。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | `oauth_service.py` schedule safe DTO | `plan_attempted_at` 误加到非 schedule DTO，真实 runner 看不到围栏 | 改到 schedule 专用白名单并做 Store→DTO→client 复核 | 已修复 |
| CR-002 | P1 | `service.py` capacity replay | 最终容量相等即可，允许用更旧候选事后凑满并跳过最新行 | 按 FIFO 遍历维护语言计数，要求跳过点之前已满 | 已修复 |
| CR-003 | P1 | pool proof / historical errors | historical available 错误导致 proof `updated_count=0`，形成新循环 | 保留 NONBLOCKING/REVALIDATABLE 审计码，以独立 source 水合时间消费容量证明 | 已修复 |
| CR-004 | P2 | source connection error | connect/close 异常可能带主机或凭据进入错误信息 | 统一转安全 `CandidateQueryError`，close 异常不覆盖根因 | 已修复 |
| CR-005 | 测试增强 | bound drama recovery | mixed published/failed 与 relay 恢复需固化 | 增加原队列/计数/relay ledger 无新增队列断言 | 已完成 |
| CR-006 | P1 | drama recovery env loader | 生产 schedule env 的两个非敏感现行键未进安全 allowlist，validate-only 会失败关闭 | 仅加入候选分页和单批修复上限；敏感键继续拒绝 | 已修复 |

## 编译 / 验证结果

- `py_compile`：通过。
- 变更聚焦测试：181/181 通过。
- 完整 X 测试：796/796 通过，2 项预期跳过。
- 独立 DTO/fence 三模块：226/226 通过；recovery store：102/102 通过。
- `git diff --check`：通过（仅 Windows 换行提示）。
