# SA 测试用例评审

## 结论

有条件通过。用例已覆盖功能主链路和主要并发/恢复风险；最终发布前须提供自动化结果、生产只读状态与一条“不发布”的 ready canary 证据。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | TC-001/004 | 仅测响应快不够，可能仍异步触发了错误的同步 GPU 调用。 | 使用 Fake GPU 断言 preview/add 返回时 prepare 调用分别为 0；add 后仅 runner process 才为 1。 | 已补充 |
| STR-002 | TC-011/012 | 需证明“账号 FIFO”不是全局串行。 | A1/A2+B1/B2 同时测试账号队首候选。 | 已补充 |
| STR-003 | TC-014/015 | 续租测试未必覆盖 ABA。 | worker-2 reclaim 后使用 worker-1 旧 token complete，必须失败且不写 pool。 | 已补充 |
| STR-004 | TC-016/018 | 需证明完成的跨表原子性。 | 在冲突/异常注入后断言 intake 未 ready 且 recurring 无孤儿行。 | 已补充 |
| STR-005 | TC-019/020 | 需区分可重试 5xx 与终态 4xx/元数据错误。 | 分别断言 retry_wait 与 failed。 | 已补充 |
| STR-006 | TC-025 | 合并列表可能重复显示关联 ready 行。 | 断言 intake ready 与 linked recurring 只显示一次。 | 已补充 |
| STR-007 | TC-037 | 生产 canary 易误触真实发布。 | gates 全关，禁止 run-now/due publish，只做到 ready 并核对无 publish ID。 | 已补充 |

## QA 修订确认

QA 执行时必须记录：测试命令、用例数、失败数、临时数据库路径、生产 gate 值、canary intake/material ID、ready pool ID、systemd 状态和未产生 TikTok 发布记录的查询证据。
