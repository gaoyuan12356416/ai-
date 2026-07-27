# SA 代码评审

## 结论

**代码评审、Linux 门禁与生产验收通过。** 最终全量审计无剩余 P0/P1/P2。生产运行版本可追溯到 commit `e77dba9c5d742e5e982c3faa44e9303761f0ff0b`，release、备份、运行文件哈希和功能证据均已核对。

## 实际评审范围

- 固定 W2A 源客户端、HTML parser、允许的 host/path、重定向拒绝和大小写敏感的实际 `content_id` 校验。
- SQLite 正缓存、负缓存、stale、跨进程租约、`(landing_id, content_id)` 复合主键及 connect 前后存储身份复查。
- `app.py` 的 `TT_DRAMA_RESOURCE_SOURCE` 共享服务选择、公开响应字段白名单、内部/公开缓存状态映射及旧 MySQL 回退路径。
- 预热任务的固定 insight 表/索引、三日只读候选查询、5001 门禁、保持花费排名的 cursor v2、bounded retry backlog、fresh 跳过、`--dry-run`、4 worker/2 QPS 和 500/3000 硬上限。
- featured 任务的花费排名、SQLite/W2A 资源复用、无剧集元数据查询、last-known-good 原子发布及 180 秒超时。
- systemd unit/timer、主 API drop-in 不设置全局 UMask、state 目录固定 `install`、SQLite DB/WAL/SHM 局部 `0660`、共享 release/current 路径、数据盘目录和回滚配置。

## 问题清单

| 编号 | 严重级别 | 问题 | 处理结果 | 状态 |
| --- | --- | --- | --- | --- |
| CR-001 | P1 | 源地址或原始链接可能进入缓存/API，扩大内部实现暴露面 | 仅在请求时临时构造 W2A URL；SQLite 和公开响应均不保存 `source_url`、原始 HTML 或原始落地页链接 | 已解决 |
| CR-002 | P1 | 仅以 `content_id` 建键会在不同 `landing_id` 间串缓存 | 资源缓存和租约均改为 `(landing_id, content_id)` 复合主键；当前业务固定 `landing_id=2049` | 已解决 |
| CR-003 | P1 | 描述为空与页面结构缺失未区分 | `.info .desc` 元素必须存在，但元素文本允许为空；缺少元素按结构异常处理 | 已解决 |
| CR-004 | P1 | 每轮固定取头部会饿死尾部，按 ID 重排又会破坏花费优先级；无限失败重试会挤占正常轮转 | cursor v2 保持 SQL 花费排名，以 `next_content_id` 接续、目标消失时以 `next_index` 兜底；retry backlog 最多 5000、每轮最多重试 100 且保留正常轮转位置。fresh 命中跳过源站，`--dry-run` 不写状态也不访问 W2A | 已解决 |
| CR-005 | P1 | 产品、来源、表或索引可配置漂移会扩大候选范围或触发错误执行计划 | 候选查询固定 `kunlunads_dev.ads_custom_source_insight`、索引 `as`、Dramawave、TT、`data_source=6` 与 `landing_id=2049`，并在 LIMIT 前过滤；普通任务硬上限 500，只有显式 bootstrap 可到 3000 且从最高花费开始 | 已解决 |
| CR-006 | P1 | featured 再查剧集元数据会绕过统一资源缓存，且启动超时不足 | featured 只查询花费排名，资源走共享服务；使用共享 release/current，设置 `TimeoutStartSec=180` 并保留 last-known-good | 已解决 |
| CR-007 | P1 | 新缓存内部字段和状态可能破坏公开 API | 公开字段使用白名单；内部 `ORIGIN_FILL/NEGATIVE_FILL` 映射为公开 `MISS`，`DISK_HIT` 映射为 `HIT`，不扩大旧响应头枚举 | 已解决 |
| CR-008 | P1 | API 与离线用户创建的 SQLite/WAL/SHM 可能互相不可写；整个单体 API 的 UMask 变更会影响其他后台文件 | 从主 API drop-in 移除全局 `UMask`；state 固定创建为 `tt-drama-featured:tt-drama-featured`、`2770`，缓存模块仅对 DB、`-wal`、`-shm` 执行 `0660` 规范，失败时 fail closed | 已解决 |
| CR-009 | P0 | API 启动后数据盘掉载，原路径落回系统盘；只做启动检查或 connect 前检查仍有误写竞态 | 首次完整 mount/UUID 校验后记录父目录 `st_dev`；每次 connect 前后检查路径软链接、父目录类型和设备号，后置失败时先关闭连接再抛错 | 已解决 |

## 构建与验证结果

| 门禁 | 结果 | 证据 |
| --- | --- | --- |
| Linux TT Python | 通过 | 104/104 |
| TT Node | 通过 | 53/53 |
| X pool / X routes 回归 | 通过 | 7/7、14/14 |
| Python 编译/兼容检查 | 通过 | `compileall`、Python 3.9 AST 均 OK |
| 补丁格式检查 | 通过 | diff-check OK |
| 生产有效 ID canary | 通过 | `Ag0rfr5F0F` 返回 `Her Beast` 与 CDN 封面 |
| 生产错误/短 ID canary | 通过 | `ZZZZZZZZZZ` 为 `404 MISS` 后 `404 NEGATIVE_HIT`；短 ID 为 `400 BYPASS` |
| 生产缓存性能 | 通过 | 30 次 HIT，p95 `14.162 ms` |

## 生产发布与验收证据

- Commit：`e77dba9c5d742e5e982c3faa44e9303761f0ff0b`
- Release：`/mnt/data-disk/tt-drama-resource-cache/releases/ai-tt-w2a-cache-e77dba9c5d74`
- 备份：`20260727T092255Z-predeploy`、`20260727T094144Z-concurrent-x-baseline`
- 生产 app SHA-256：`ac68d0cc7c4b58ce9a242b6c12d6b45391b57f847a5e0801aff30d6f69310398`
- 跨用户 SQLite/WAL 权限、systemd timer、featured 5 项、prewarm 500 部和真实浏览器均通过。

## 评审建议

本次生产 release 验收通过。保留两份备份和 release manifest，持续监控 W2A 结构、SQLite/WAL、预热 timer 与 featured last-known-good。
