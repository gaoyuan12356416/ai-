# 缺陷登记

## 当前状态

多轮独立代码评审发现的问题已修订，并通过最终 395 项全量回归、独立收口复审、生产候选与线上验证；当前没有未关闭的 P0/P1/P2。

## 缺陷列表

| 编号 | 级别 | 标题 | 根因与影响 | 修复 | 状态 |
| --- | --- | --- | --- | --- | --- |
| BUG-001 | P0 | 历史 pending `{url}` queue 升级后可能失败 | 历史 queue 没有新 route/code，若一律要求 route 会阻断既有待发布任务 | 无 code 时按原字段生成 AIpost long URL；有 code 才要求 route | 已关闭，全量回归通过 |
| BUG-002 | P0 | URL builder 默认 channel 漂移 | 默认改成 TT 会影响直接测试和既有调用 | 默认恢复 AIpost；新正式 queue 显式 TT | 已关闭，全量回归通过 |
| BUG-003 | P0 | 公开 resolver 绕过既有保护 | Nginx 若直达 sidecar，会缺少现有剧目校验、token bucket 和并发 gate | 公共 route 改到主 app 8787；sidecar 仅 loopback bearer 私有接口 | 已关闭，全量回归通过 |
| BUG-004 | P1 | 慢 Redis 占用 queue 写锁 | cache GET/DELETE 网络等待若持有共享锁，会阻塞正式 queue 写事务 | 两阶段失效在事务共享锁内 rotate namespace，锁外 best-effort Redis DEL；reconcile 释放共享锁后再做网络失效；新增慢读/慢删测试 | 已关闭，全量回归通过 |
| BUG-005 | P1 | 高占用空槽兜底 SQL 放大 | 逐候选 SQL 最坏超过百万次 | 一次读取 code，bytearray 位图 O(capacity) 找空槽 | 已关闭，全量回归通过 |
| BUG-006 | P1 | 新正式 URL 参数顺序不符合合同 | builder 历史 c-first 与新需求 af_dp-first 冲突 | 新正式和 clone 显式 af_dp-first；validator 保留历史兼容 | 已关闭，全量回归通过 |
| BUG-007 | P1 | Redis data dir 启动前不存在 | 候选机首次启动证实主 unit 的 mount namespace 先于 `ExecStartPre` 建立，命令以 `226/NAMESPACE` 失败 | 拆成最小权限 `tt-post` prepare oneshot，以 `RequiresMountsFor` 等待数据盘并通过 mount condition 后创建 0700 子目录；主 unit `Requires/After` prepare，仍只写 Redis 子目录 | 已关闭，exact commit 首次启动通过 |
| BUG-008 | P1 | 加法迁移可能半完成 | `executescript` 隐式提交导致 route 与 queue.code 不在同一事务 | baseline script 后显式开启新的 `BEGIN IMMEDIATE` | 已关闭，DB 副本迁移/回滚/幂等通过 |
| BUG-009 | P1 | 满池回收缺少持久审计 | 旧 code 改指向只能从现状推断 | 增加 `tt_post_code_recycle_audit`，回收同事务写审计 | 已关闭，全量回归通过 |
| BUG-010 | P0 | `{code}` formal queue exact retry 误冲突 | 首次 freeze 后 caption 已含真实 code，重放请求仍携带 deterministic pre-freeze caption，单一字符串比较误判相同 idempotency_key 为不同事实 | 其他冻结事实完全一致时同时接受 deterministic pre-freeze caption 与已冻结 code caption；新增 exact replay/差异 payload 回归 | 已关闭，全量回归通过 |
| BUG-011 | P1 | 输入修改后旧 CTA 与旧响应可复活 | input 变化没有立即撤销旧 href，也没有 abort/作废在途 resolver | input handler 立即清结果/href/data、递增序列并 abort；响应提交前校验最新序列 | 已关闭，Chrome 已复验 |

## 发布阻断条件

- 任一 P0/P1 在最终独立复审中重新打开。
- 任何当前最终 diff 测试失败或需要跳过才能通过。
- 普通碰撞触发回收、回收不写 audit，或 Redis 返回旧 namespace route。
- Redis 网络 I/O 仍占用 shared queue write lock。
- 历史 pending/direct-test AIpost 兼容回归失败。
- 公共 Nginx route 可直接到 sidecar或 internal route 可在公网访问。
- 横向拖动触发卡片跳转，或前端需发两次 resolver 请求。
- 相同 formal payload/idempotency_key 无法 exact replay，或修复错误放宽了差异 payload 的冲突保护。
- 输入变化后旧 href 仍可点击，或过期 resolver 响应能覆盖当前输入。
- 原 `/tt` 文件/路由/行为变化。
- 验收需要真实 TikTok 发布才能证明功能。

## 上线后要求

如生产验证出现新问题，使用 `BUG-012` 起连续编号，记录 exact commit/release、时间、环境、最小复现、影响、回滚/修复和复验结果；不得覆盖上述历史记录。
