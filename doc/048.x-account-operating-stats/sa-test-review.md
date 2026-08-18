# SA 测试用例评审

## 结论

通过。P0 覆盖 actor、时间边界、site/campaign、未归属、缓存降级、鉴权/no-store 和 OAuth/relay 回归。

| 编号 | 问题 | 修订 | 状态 |
| --- | --- | --- | --- |
| TR-001 | relay source 成功但目标失败也需计真实 Post | 以 source_post_id/source_published_at 为事实 | 已关闭 |
| TR-002 | 日期中点无法发现 UTC 边界错误 | 加 15:59:59Z/16:00:00Z | 已关闭 |
| TR-003 | 重复 query c 与跨账号冲突 | 两类均 fail closed | 已关闭 |
| TR-004 | cache 缺失与陈旧不同 | 分别验证空值/旧值告警 | 已关闭 |
| TR-005 | gate 仅拒绝 mysql.real 覆盖不足 | 增加 mariadb/ELF/其他路径对抗用例 | 已关闭 |
| TR-006 | failed log 可能污染 campaign map | 增加同 c 的 confirmed/failed 对抗 fixture | 已关闭 |
| TR-007 | campaign 大小写/尾空格可能被 SQL 合并 | 校验三处 binary 表达式，并经 gated-output parser/build fixture 保持三行独立 | 已关闭 |
| TR-008 | 缺失/畸形/不连续 cache 日期可能 fresh | 四类 invalid fixture 均断言 missing/null | 已关闭 |
| TR-007 | relay ledger 可与 queue 不一致 | 增加 queue 冻结字段和 mismatch fixture | 已关闭 |
| TR-008 | TTL 内跨日仍可能口径错误 | 增加北京时间跨日及未来时钟边界 | 已关闭 |

已落实到 `scripts/test_x_account_operating_stats.py`。
