# SA 代码评审

## 结论

**预发布实现代码评审与本地门禁通过。** 最终全量审计无剩余 P0/P1/P2，测试证据见 `test-report.md`；Windows 跳过的 POSIX mode preservation 必须在 Linux 补跑。该结论不代表已经提交、部署或完成生产验收。

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
| TT Python 自动化测试 | 本地通过 | 计数与平台 skip 见 `test-report.md`；Linux 补跑待执行 |
| TT Node 断言 | 本地通过 | 证据见 `test-report.md` |
| Python 编译/兼容检查 | 本地通过 | `compileall` 与 Python 3.9 AST 证据见 `test-report.md` |
| 补丁格式检查 | 本地通过 | `diff-check` 证据见 `test-report.md`；生产 release 仍需重跑 |
| 此前本地真实源 canary：有效 ID | 通过 | `Ag0rfr5F0F` 解析为 `Her Beast`，419.7 ms |
| 此前本地真实源 canary：错误 ID | 通过 | 请求 `ZZZ…` 时源页实际解析为 `Yqq…`，因精确 ID 不匹配被拒绝，332.0 ms |

## 尚未完成的发布证据

- 尚无 GitHub commit、push、release 或生产部署证据。
- 尚未在生产数据盘验证目录、权限、SQLite WAL、租约和重启持久化。
- 尚未验证生产 systemd unit/timer、journal、错峰执行和 last-known-good。
- 尚未完成公开 API、生产性能与 `/tt` 真实浏览器回归。

## 评审建议

实现代码评审和本地门禁已通过，具备进入 GitHub-first 部署流程的条件。生产发布仍必须在 Linux 验证 POSIX mode preservation 和 connect 前后设备身份保护，并执行部署文档中的备份、release/current 切换、systemd、HTTP canary、浏览器和回滚门禁；在这些证据完成前不得标记为“已上线”。
