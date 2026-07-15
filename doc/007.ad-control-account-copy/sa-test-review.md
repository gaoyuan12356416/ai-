# SA 测试用例评审

## 结论

用例覆盖主要业务分支、生产复制前置熔断和既有 pause 回归，可进入实现。本期只允许生产观察验收，不允许复制 Canary。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| TR-001 | 观察模式 | 只测返回值不能证明零业务副作用 | 断言 Meta writer、created_data/lineage writer 调用 0 次；既有 action log 允许写审计 | 已补充 |
| TR-002 | 正式 copy | 熔断误开可能越过持久化前置 | 无论熔断值如何，均断言 `copy_persistence_not_configured` 且 Meta POST 0 次 | 已补充 |
| TR-003 | 纯编排模块 | 后续状态机仍需验证调用顺序 | 仅用 Stub 测 PAUSED、轮询、映射和幂等，不接入 app/runner | 已补充 |
| TR-004 | 权限 | 需覆盖列表/更新/删除/启停 | 双用户矩阵，均返回 owner_forbidden/not_found | 已补充 |
| TR-005 | 旧功能 | copy 熔断可能误伤 pause | 增加旧 Campaign pause 完整回归 | 已补充 |
| TR-006 | 剧目范围 | created_data 无直接剧目标识 | 通过明确映射表解析，缺失/歧义时 fail-closed | 已补充 |

## QA 修订确认

测试数据全部使用临时 SQLite、只读 SQL spy 和 Stub Meta；任何测试命令不得读取生产 token、连接生产写节点或执行复制结果 MySQL DDL/DML。
