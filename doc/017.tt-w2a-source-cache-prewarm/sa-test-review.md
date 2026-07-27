# SA 测试用例评审

## 结论

通过。测试设计已覆盖“HTTP 200 但实际 ID 不一致”、源故障不得负缓存、数据盘持久化、跨进程租约、3 日轮转预热、接口兼容、featured LKG 和长驻 API connect 前后设备身份复查。Linux TT Python 104、Node 53、X pool 7、X routes 14、`compileall`、Python 3.9 AST 和 diff-check 均通过；生产 API、数据盘权限、timer、性能、featured、prewarm 和真实浏览器也已通过。最终审计无剩余 P0/P1/P2。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 处理 | 状态 |
| --- | --- | --- | --- | --- |
| TR-001 | TC-002 | 仅验证结果不能证明没有加载页面资源 | 增加请求记录并断言只有 HTML GET | 已补充 |
| TR-002 | TC-006/007/008 | 不存在、默认回退和结构异常不能混为 404 | 分别验证负缓存与 503 不负缓存 | 已补充 |
| TR-003 | TC-019/020/021 | 仅测线程 single-flight 不覆盖 API/timer 跨进程竞争 | 增加 SQLite 租约、等待和过期接管 | 已补充 |
| TR-004 | TC-023 | 路径位于 `/mnt/data-disk` 不等于数据盘真实挂载 | 增加挂载、设备、UUID、空间和软链接门禁 | 已补充 |
| TR-005 | TC-026/027/029 | “近 3 日投放中”需可审计且有规模保护 | 固定上海自然日、查询条件、二进制 ID 和 5001 门禁 | 已补充 |
| TR-006 | TC-024/025/033/034 | 新资源源可能破坏旧 API、参数透传或 featured LKG | 增加完整兼容与失败回归 | 已补充 |
| TR-007 | TC-038 | 只测功能不足以证明 GitHub-first 可追溯和具备回滚点 | 增加 commit、release、manifest、备份和运行哈希核对 | 已补充并通过 |
| TR-008 | TC-024 | 内部缓存状态可能扩大公开 API 枚举 | 断言 `ORIGIN_FILL/NEGATIVE_FILL -> MISS`、`DISK_HIT -> HIT` | 已补充 |
| TR-009 | TC-023/032 | 只验证目录存在不足以证明 API 与离线任务可共享 SQLite；全局 UMask 会误伤单体后台其他模块 | 增加“API drop-in 无全局 UMask”、state 固定 `install`/owner/group/`2770`、DB/`-wal`/`-shm` `0660` 检查 | 已补充 |
| TR-010 | TC-026/028/031 | 候选重排、候选变化与失败积压可能破坏花费优先级或轮转公平性 | 增加固定表/索引、保持花费排名、cursor v2 `next_content_id`/`next_index` 接续、有界重试和 500/3000 硬上限 | 已补充 |
| TR-011 | TC-023 | 启动时 mount/UUID 通过不能覆盖长驻 API 运行中掉载，单一 connect 前检查还有竞态窗口 | 增加首次记录 parent `st_dev`、每次 connect 前后复查软链接/父目录/设备号、变化关闭连接并 fail closed 的测试 | 已补充并通过 |

## QA 修订确认

38 个验收用例全部通过。Linux TT Python 104 项、Node 53 条、X pool 7 项、X routes 14 项均通过，Windows 上无法执行的 POSIX mode preservation 已由 Linux 与跨用户 SQLite/WAL canary 覆盖。真实浏览器中错误 ID 只产生预期接口 `404`，console 无 JavaScript exception，也无 CSP warning/error；封面、标题、简介、错误 ID 隐藏卡片及 `af_adset_id=XXX` 透传均通过。生产 release、备份、app hash、API、timer、featured、prewarm 和性能证据见 `test-report.md`。
