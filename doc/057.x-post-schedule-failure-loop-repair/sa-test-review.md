# SA 测试用例评审

## 结论

通过。P0 用例必须同时断言零 X、零未知覆盖、身份不漂移和失败时零队列。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| TR-001 | FIFO 容量 | 仅测“能通过”不足以防伪 | 增加无证据、错误证据、未满证据 | 已补 |
| TR-002 | plan unknown | 必须证明不二次写 | 增加一次性 plan_attempt fence 和下一 tick 测试 | 已补 |
| TR-003 | stale lease | updated_at 会污染 FIFO cutoff | 使用独立 heartbeat 字段并断言 updated_at 不变 | 已补 |
| TR-004 | 短剧恢复 | 必须覆盖部分清单/绑定漂移/attempt/unknown | 加 store 原子性与 CLI orchestration 用例 | 已补 |

## QA 修订确认

已纳入 schedule runner、multi schedule store、selector 和 bound drama recovery 测试。
