<!-- context-entry:social-report-query-and-wal-reliability-20261029 -->
### 社媒报表查询并发、失败日期与 SQLite 锁
Status: active
Updated: 2026-10-29
Tags: social-analytics, reliability
Replaces: 

Details:
- 社媒报表 HTTP 广域 YouTube/X 查询使用精确站点/日期/归因 ID 分批和最多 2 个并发读取，仍经 SQL FIFO 门禁，40 秒总预算；网页最多 2 个独立渠道并发，每请求最多 4 条 SQL，网页单条查询上限 12 秒；重复 SQL 合并和有界短缓存，不缓存失败，失败不作零。
- Facebook 网页查询最多四天一窗，最多 2 个并发；SQL 按完整二进制 Campaign/Page/素材分组后，在源返回前用冻结三元组集合精确过滤，不能仅按 Campaign 汇总或将三个 ID 集合独立匹配。任何窗失败或超限都失败关闭，保留来源日期覆盖哨兵。
- 读取失败日期标为 unverified_dates，只有可核实缺失才标 missing_dates；保留租户/人员 ACL、冻结归属及整数金额守恒。
- 不得在 SQLite 活连接期间以普通文件描述符单独开关 WAL/SHM；权限收紧使用元数据操作，保持文件锁。Facebook 采集模块与主 API 读模块需兼容同步。
- 维护代码和回滚：GitHub gaoyuan12356416/ai-，ops/social-report-reliability-20261029；只部署报表模块/静态文件及单模块采集器新版本，不触发发布或改定时调度。
<!-- /context-entry:social-report-query-and-wal-reliability-20261029 -->
