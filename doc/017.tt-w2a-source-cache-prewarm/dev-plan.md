# 开发计划

## 开发范围

建立共享 W2A 资源模块，将现有 resolver 和 featured 接入数据盘 SQLite，并新增最近 3 日投放剧预热任务。现有公开 URL、前端跳转和追踪参数契约不变。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 资源数据模型与错误分类 | `features/tt_drama_resources/models.py` | 已完成 |
| 原始 HTML 解析与精确 ID 校验 | `features/tt_drama_resources/parser.py` | 已完成 |
| 固定 W2A HTTP 客户端 | `features/tt_drama_resources/client.py` | 已完成 |
| SQLite schema、TTL、租约、过期判定和运行期存储身份复查 | `features/tt_drama_resources/cache.py` | 已完成 |
| 共享按需/预热服务 | `features/tt_drama_resources/service.py` | 已完成 |
| resolver 选择与应用装配 | `app.py`、`.env.example` | 已完成 |
| 最近 3 日候选、cursor v2 轮转/重试与预热 | `features/tt_drama_prewarm/`、`scripts/prewarm_tt_drama_resources.py` | 已完成 |
| Featured 复用资源缓存 | `features/tt_drama_featured/`、`scripts/refresh_tt_drama_featured.py` | 已完成 |
| systemd oneshot/timer 文件 | `deploy/tt-drama-resource-prewarm.*`、`deploy/tt-drama-featured.service` | 已完成并通过生产验证 |
| 单元、集成和回归测试 | `tests/test_tt_drama_*.py`、Node TT/X 回归 | 已完成并通过 Linux/生产验证 |
| SA 代码评审与测试报告 | `doc/017.tt-w2a-source-cache-prewarm/` | 已完成 |
| GitHub-first 提交、生产部署与回滚点 | GitHub、CPU 服务器 | 已完成 |

## 实现顺序

1. 已完成标准库 HTML parser、模型和测试样本。
2. 已完成不跟随重定向、无重试的固定源客户端及超时、大小、内容类型和域名保护。
3. 已完成 SQLite 正/负/stale 缓存、复合主键、跨进程租约，以及首次完整 mount/UUID 校验后记录父目录 `st_dev`、每次 connect 前后复查软链接/父目录/设备号的 fail-closed 保护。
4. 已由 `app.py` 通过 `TT_DRAMA_RESOURCE_SOURCE=w2a_cache` 选择共享 resolver，并把内部 `ORIGIN_FILL/DISK_HIT/NEGATIVE_FILL` 映射回公开 `MISS/HIT` 兼容语义。
5. 已完成固定 `kunlunads_dev.ads_custom_source_insight`/`as` 索引的只读候选查询、保持花费排名且以 `next_content_id`/`next_index` 接续的 cursor v2、最多 5000 条的失败积压/每轮最多 100 条重试、新鲜缓存跳过、普通任务硬上限 500 及仅显式 bootstrap 可到 3000；bootstrap 从最高花费候选开始。
6. 已将 featured 资源读取接到共享服务，MySQL 只保留昨日花费排名并继续使用 LKG。
7. 已补齐并安装环境配置和 systemd 文件；主 API drop-in 不设置全局 `UMask`，state 目录由固定 `install` 创建为 `tt-drama-featured:tt-drama-featured`/`2770`，缓存模块只把 DB、`-wal`、`-shm` 规范为 `0660`；生产定时任务与跨用户权限 canary 通过。
8. 已完成 SA 评审、本地/Linux 门禁、GitHub-first release、生产 API/性能/浏览器验证和回滚点记录。

## 构建与验证结果

最终门禁全部通过：

- Linux TT Python 104、Node 53、X pool 7、X routes 14。
- `compileall`、Python 3.9 AST、diff-check 均为 OK。
- 有效 `Ag0rfr5F0F` 返回 `Her Beast` 与 CDN 封面；错误 `ZZZZZZZZZZ` 首次 `404 MISS`、再次 `404 NEGATIVE_HIT`；短 ID 为 `400 BYPASS`。
- 30 次 `HIT` 的 p95 为 `14.162 ms`。
- Featured、prewarm、timer、跨用户 SQLite/WAL 权限和真实浏览器均通过生产验收。

完整证据见 `test-report.md` 和 `deploy.md`。

## 风险与依赖

- 依赖 W2A 原始 HTML 保持可解析；解析结构变化必须 fail closed。
- 依赖 63350 只读数据源选取预热候选，不允许写入远端数据库。
- API 与预热进程都需对同一 SQLite 目录具备最小读写权限。
- 发布前必须验证数据盘真实挂载和剩余空间，不能因目录不存在回退到根盘。
- 长驻 API 不能只在启动时验证数据盘；缓存连接前后必须持续核对父目录 `st_dev` 和软链接/目录身份，防止运行中掉载后误写根盘。
- 当前使用 Python 标准库解析源码，不引入浏览器运行时或额外 HTML 渲染依赖。

## 完成记录

需求已完成生产发布与验收。生产 commit 为 `e77dba9c5d742e5e982c3faa44e9303761f0ff0b`，release 为 `/mnt/data-disk/tt-drama-resource-cache/releases/ai-tt-w2a-cache-e77dba9c5d74`；备份、运行文件哈希和验收明细见 `deploy.md`、`test-report.md`。
