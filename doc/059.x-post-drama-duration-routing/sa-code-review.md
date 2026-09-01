# SA 代码评审

## 结论

通过。独立审查发现的代码 P0/P1 均已关闭；生产自然发布结果不在代码评审中代验收。

## 评审范围

- schema/migration/triggers、resolver 事务和 route immutability。
- 媒体下载/repair policy/final evidence/prepared capability。
- 发布调用顺序、Token/X write 边界与 unknown 幂等。
- fixed/random runner、waiting 跨周期、DTO/UI 与历史 141 回归。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | due limit | 旧 waiting run 固定排在前面会饿死后续批次 | store 内精确 limit + heartbeat 公平游标 | 已修复/回归 |
| CR-002 | P1 | drama assignment | waiting 精确剧集会被下一 slot 重选并回滚健康 sibling | 账号占用但 held episode 不输出 | 已修复/回归 |
| CR-003 | P1 | DB trigger | waiting 已冻结媒体仍可被 SQL 改写 | 冻结 URL/SHA/size/duration/repair/dimensions | 已修复/篡改回归 |
| CR-004 | P1 | crash resume | route/reserve 提交后首次 X attempt 前跨日会被 stale-stop | shared resumable predicate 覆盖 no-log 与 reserved/attempt0 | 已修复/4 条回归 |
| CR-005 | P1 | relay ledger | resolved relay ledger 可删除或搬移 | scoped delete/identity immutability trigger | 已修复/篡改回归 |
| CR-006 | P1 | rollout | feature-off 恢复 timer 会产生旧 141，新迁移遗漏 X Auto/Main writer | 全 writer 排空；双端 true 后恢复原状态 | 已修订 |
| CR-007 | P2 | admin DTO | logs 下发 UI 未使用的 SHA/size/internal mode | 仅展示路线、时长、宽高 | 已修复/contract |

## 编译 / 验证结果

`python -m compileall -q features scripts` 与 `git diff --check` 通过；store 128/128、全量 X 888 项通过、2 项环境跳过。未解析 route 无 publish log，只有 resolved relay 有 ledger，resolved 路线/relay/媒体由应用与 DB 双围栏冻结，新 pending 不使用 141。全部平台写操作均为 mock，未创建真实 Post/Repost。
