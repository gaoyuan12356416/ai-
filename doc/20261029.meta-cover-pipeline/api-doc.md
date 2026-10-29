# 接口与数据兼容

不新增对外 HTTP API，不变更 Meta 广告业务参数契约。沿用 CLI cover.py ... work 和持久队列。

性能日志增加执行循环标记，报表兼容旧记录。具体字段和新增配置默认值在最终评审及部署记录确认。数据库不清空、不重建，回滚保留最新业务账本。
## 最终实现语义

- 新配置 cover_cleanup_workers 默认4，仅允许1..4。两个lane继续共用总32、账户2、24req/s预算。
- 常驻 work 启动两个持续循环；每小时协作排空后在无活跃lane时只读核实未知结果。
- work --once 表示广告和清理lane各执行一个周期；不保证本次广告新入队的清理任务在该次运行中全部清理，常驻模式持续消费持久队列。
- 性能JSON新增 lane（ads/cleanup）、execution_started_at，保留result.seconds；旧记录按legacy解释。报表通过活跃区间并集计算总吞吐，并保留各lane耗时和数量。
- 来源批次上限64，上下文TTL保持原值（生产30秒）；fresh buffer未消耗完且执行槽已满时不会逐条补SQL。
- SQLite仅增加tasks_creative_status索引，旧代码仍可读取；回滚保留索引及最新业务账本。