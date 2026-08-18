# SA 测试用例评审

## 结论

通过。P0 覆盖 actor、时间边界、site/campaign、未归属、缓存降级、鉴权/no-store 和 OAuth/relay 回归。

| 编号 | 问题 | 修订 | 状态 |
| --- | --- | --- | --- |
| TR-001 | relay source 成功但目标失败也需计真实 Post | 以 source_post_id/source_published_at 为事实 | 已关闭 |
| TR-002 | 日期中点无法发现 UTC 边界错误 | 加 15:59:59Z/16:00:00Z | 已关闭 |
| TR-003 | 重复 query c 与跨账号冲突 | 两类均 fail closed | 已关闭 |
| TR-004 | cache 缺失与陈旧不同 | 分别验证空值/旧值告警 | 已关闭 |

已落实到 `scripts/test_x_account_operating_stats.py`。
