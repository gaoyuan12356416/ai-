# 测试报告

## 测试结论

本地 QA 与生产只读验收均通过。focused 证明 codec/dimensions 两类错误均能重制并冻结完整台账；X 全量回归无失败；生产自然 timer 验收未创建测试 Post。

## 测试范围

material schedule preflight/repair、语言/FIFO、跨页补位、frozen recovery、drama 不变和 X 全模块。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| schedule + relay focused | 55 | 55 | 0 | 0 |
| X 全量自动化 | 752 | 750 | 0 | 0（条件跳过 2） |
| 服务器 focused | 55 | 55 | 0 | 0 |
| 生产只读验收 | 1 组 | 1 | 0 | 0 |

## 缺陷情况

- BUG-001：已修复，本地 QA 已关闭。

## 验证证据

- codec/dimensions 均触发 repair。
- 输出 URL、SHA256、size、width、height 和 repair trigger/job 均被断言。
- `Ran 752 tests in 54.833s`，`OK (skipped=2)`。
- 服务器 `Ran 55 tests in 0.481s`，`OK`。
- 自然 schedule tick 为 `no_due`，自然 claim tick 为零 claim；queue/log 保持 `606/606`，unknown/active 均为 `0`。
- current release 文件 Git blob 与提交一致；GPU repair health/profile 正常。

## 遗留风险

- 到点预检耗时可能增加；不影响 unknown/no-duplicate 门禁。
- 历史 run 318/320 的失败行不在本次自动恢复范围。

## 发布建议

已完成 GitHub-first 部署与生产只读验收；继续保持不以真实 Post 做部署测试。
