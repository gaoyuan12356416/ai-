# SA 测试用例评审

## 结论

通过。P0 已覆盖稳定/可注入随机、边界值、语言、无 relay 原子失败、状态机、恢复冻结及三类未授权扩面。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | 随机 | 不能用概率断言 | 提供 injected shuffler 与固定 seed 重复断言 | 已解决 |
| STR-002 | 原子性 | 只测异常码不足 | 同时断言 queue count=0 | 已解决 |
| STR-003 | 发布成功 | source Post 与 Repost 状态易混 | 分阶段读取 pool 状态 | 已解决 |
| STR-004 | 回归 | drama/manual/X Auto 可能被通用 relay 放宽污染 | 专项拒绝 manual/short，复用 drama/X Auto 全量回归 | 已解决 |

## QA 修订确认

test-cases.md 已列出 22 个验收场景；专项测试不调用网络。
