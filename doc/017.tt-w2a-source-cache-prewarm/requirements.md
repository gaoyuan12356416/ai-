# 017.tt-w2a-source-cache-prewarm 需求与技术设计

## 背景

变更前 `/tt` 搜索接口从远端剧库读取标题、简介和封面，并只保存在进程内缓存。当前实现已改为直接 GET 固定 W2A 落地页的原始 HTML，从源码提取封面、标题和描述；无需打开浏览器、执行 JavaScript 或加载图片等页面资源。

W2A 对错误 `content_id` 也可能返回 HTTP 200，并渲染另一部默认剧。因此，只有源码中解析出的实际剧 ID 与请求 ID 大小写完全一致时，才允许缓存、展示和生成跳转链接。

## 目标

- 缓存未命中时，GET `https://www.dramawavew2a.com/ads/0/2049/view?af_dp={content_id}` 的原始 HTML。
- 从源码提取剧名、描述和 CDN 封面地址，不执行页面脚本、不下载封面。
- 通过源码深链中的实际剧 ID 做大小写敏感的精确校验，阻止错误 ID 跳转。
- 使用数据盘 SQLite 持久缓存，服务重启后仍可命中。
- 定时预热最近 3 个上海自然日内仍有花费的 Dramawave W2A 剧。
- 保持现有 `/api/public/tt-drama/resolve` 和 `/api/public/tt-drama/featured` 对外契约兼容。
- `Recently featured` 排名仍来自只读花费数据，展示资源改为复用同一份 W2A 资源缓存。

## 范围

### 包含

- 固定 W2A host、path 和参数模板的服务端 HTTP GET。
- 原始 HTML 大小上限、超时、状态码和内容类型校验；单次请求不自动重试。
- 资源提取规则：
  - 封面优先取 `#topReading[data-src]`，备用取 `#image[src]`；
  - 标题取 `h1.title`；
  - 描述取 `.info .desc`。
- 从页面源码中的 DramaWave 深链解析实际 `id`，与请求 `content_id` 做区分大小写的完全相等校验。
- SQLite 正缓存、负缓存、旧值兜底、跨进程租约、过期判定及长驻进程存储设备身份复查。
- API 按需回源、同 ID single-flight、全局并发与频率限制。
- 最近 3 日投放剧候选读取、普通任务每轮硬上限 500 部、仅显式 bootstrap 可到 3000 部，以及 systemd timer。
- featured 任务复用资源缓存，同时保留 last-known-good 公共快照。

### 不包含

- 不使用 `view-source:` 作为请求协议；该前缀只是浏览器查看源码的界面功能。
- 不使用 Playwright、Chromium 或其他浏览器渲染。
- 不维护第二份进程内数据缓存；进程内只保留同 ID single-flight 协调状态。
- 不持久化完整 HTML、完整深链或临时拼出的源 URL。
- 不执行落地页 JavaScript，不加载 CSS、图片、视频、Pixel、SDK 或 OneLink。
- 不缓存完整 HTML、视频地址、剧集播放地址、广告归因参数、Pixel ID、用户 IP 或 UA。
- 不把封面图片下载或代理到 CPU 服务器；浏览器继续直接访问允许的 CDN。
- 不修改 W2A 模板、投放状态、预算或远端业务数据。
- 不新增远端数据库表或执行远端 DDL/DML。

## 业务规则与流程

### 搜索按需回源

1. 校验 `content_id` 符合 `[A-Za-z0-9_-]{10,32}`，不做大小写转换。
2. 直接查询数据盘 SQLite；新鲜记录不访问 W2A。
3. 新鲜正缓存直接返回；新鲜负缓存返回 404。
4. 缓存未命中或已过期时，为该 ID 获取跨进程租约；其他请求短暂等待结果。
5. 对固定 W2A URL 发起普通 GET，只读取有限大小的 HTML 响应体。
6. 先解析实际剧 ID；缺失或无法解析属于上游结构异常，不写负缓存。
7. 实际 ID 与请求 ID 不一致时返回 404，并写短期负缓存。
8. 精确一致后提取标题、描述和封面 URL；标题、封面及 `.info .desc` 元素必须存在，描述元素的文本允许为空。
9. 完成字段裁剪、HTTPS/CDN 白名单校验后，在单个 SQLite 事务中写入正缓存并返回。
10. 回源失败时优先返回未超过兜底期的旧正缓存；没有旧值则返回 503。

### 定时预热

1. 只读查询上海时区最近 3 个自然日（含今天）的去重投放剧。
2. 固定条件：
   - 表固定为 `kunlunads_dev.ads_custom_source_insight`，索引固定为 `as`，配置不得扩展到其他表或索引
   - `product='Dramawave'`
   - `app_id='[w2a]drama-double'`
   - `data_source=6`
   - `data_source_id` 为合法、非空、大小写精确的 Content ID
   - 按 Content ID 聚合后 `SUM(spend) > 0`
3. 候选查询最多读取 5001 个 ID；超过 5000 个时整轮失败并记录告警，不静默截断。
4. 候选保持 SQL 的花费降序，不按 ID 重新排序；普通任务使用 schema v2 的 `/mnt/data-disk/tt-drama-resource-cache/state/prewarm-cursor.json` 轮转。cursor 同时保存 `next_content_id` 和 `next_index`：目标 ID 仍存在时从该 ID 接续，ID 消失时使用有界的索引位置兜底，避免反复只处理头部。
5. 已有新鲜 SQLite 记录的候选直接命中并跳过源站访问；其余候选复用公开 resolver 的客户端、解析器、校验器和跨进程租约。
6. 普通任务每轮硬限制为 500 部，即使环境变量或命令行传入更大值也不得突破；只有显式 `--bootstrap` 可将上限提升到 3000 部。
7. schema v2 cursor 保存有界失败重试队列：积压最多 5000 个 ID，每轮最多优先重试 100 个，同时至少为正常轮转保留位置；已经成功或已退出活动候选集的 ID 从队列移除。
8. 普通任务最多 4 个 worker、全局最多 2 次源站请求/秒；`--dry-run` 只查询和规划，不访问 W2A，也不写 SQLite 或 cursor。
9. timer 基准为上海时间 `00/04/08/12/16/20:20`，任务为 oneshot，使用独占锁禁止重叠。
10. 花费仅用于候选排序，不进入资源缓存、公共 JSON、DOM 或跳转 URL。

### Featured 集成

- 昨日高花费 Top 5 的排名和 last-known-good 规则保持不变。
- MySQL 只查询昨日花费排名，不再访问 `ads_drama_resource` 获取标题、封面或语言。
- 排名任务对候选 ID 使用共享资源服务读取标题和封面；新鲜缓存命中时不访问 W2A。
- 资源缺失时允许通过共享服务回源，但必须通过精确 ID 校验。
- 本轮不足 5 个有效剧或刷新异常时，不覆盖上一份完整 featured 快照。
- 公共 featured JSON 字段、数量、隐私约束和点击前 resolver 校验保持不变。

## 技术设计

### 影响模块

- `features/tt_drama_resources/`：模型、源码解析、固定源客户端、SQLite 缓存和共享资源服务。
- `app.py`：通过 `TT_DRAMA_RESOURCE_SOURCE=w2a_cache` 选择共享资源服务，保留路由层限流、并发门禁和公开字段白名单。若来源选择值不支持，或 W2A 装配配置校验失败（包括 `TT_DRAMA_RESOURCE_LANDING_ID` 非 `2049`），记录错误并回退旧 MySQL resolver，不能让单体 API 在导入阶段崩溃。
- `features/tt_drama_featured/`：排名不变，元数据来源改为共享资源服务。
- `scripts/prewarm_tt_drama_resources.py`：最近 3 日候选查询和分批预热入口。
- `deploy/tt-drama-resource-prewarm.service`、`deploy/tt-drama-resource-prewarm.timer`：离线调度。
- `app.py`、`.env.example`：依赖装配和配置示例。
- `tests/test_tt_drama_resources_*.py` 及现有 resolver/featured 回归测试。

### SQLite

生产路径固定为：

```text
/mnt/data-disk/tt-drama-resource-cache/state/resources.sqlite3
```

核心表：

- `tt_drama_resource_cache`
  - 复合主键：`(landing_id, content_id)`
  - `landing_id`
  - `content_id`
  - `status`：`ready` / `not_found`
  - `resolved_content_id`
  - `title`
  - `description`
  - `cover_url`
  - `content_hash`
  - `fetched_at`
  - `fresh_until`
  - `stale_until`
  - `last_error_code`
  - `last_error_at`
  - `updated_at`
- `tt_drama_resource_lease`
  - 复合主键：`(landing_id, content_id)`
  - `owner`
  - `lease_until`
- schema/meta 表：记录 schema 和 parser 版本。

SQLite 使用 WAL、busy timeout 和短事务；租约过期后可安全接管。禁止把数据库、WAL、SHM 或临时文件落到根盘。

长驻 API 的存储身份保护：

- 首次初始化必须完成完整的数据盘路径、独立挂载、精确 mountpoint、UUID、空间、可写性和软链接校验；通过后记录 SQLite 父目录的 `st_dev`。
- 每次调用 `sqlite3.connect` 前必须重新检查完整路径中不存在软链接、父目录仍存在且为真实目录，并确认父目录 `st_dev` 与首次记录值一致。
- `sqlite3.connect` 返回后、执行任何 PRAGMA/SQL 前再次执行同一检查，覆盖“预检通过后、连接建立前数据盘丢失”的竞态窗口。
- 任一次前置或后置检查发现软链接、父目录或设备号变化，立即关闭刚建立的连接并 fail closed；不得在系统盘同名目录创建或写入 SQLite。

默认缓存策略：

- 正缓存新鲜期固定为 24 小时，不增加 TTL 随机抖动。
- 正缓存旧值兜底期 7 天。
- 精确 ID 不匹配或明确 404 的负缓存 15 分钟。
- 超时、429、5xx、HTML 超限、解析失败和 SQLite 错误不得写成 `not_found`。

### HTTP 源约束

- 固定 scheme：`https`
- 固定 host：`www.dramawavew2a.com`
- 固定 path：`/ads/0/2049/view`
- 唯一动态值：通过格式校验的 `af_dp`
- 默认总超时 5 秒，HTML 最大 512 KiB。
- 只接受目标 URL 返回的成功 HTML 响应；客户端不跟随重定向。
- 不接收客户端传入的 URL、host 或 path，避免 SSRF。
- 封面只接受 HTTPS 和配置白名单域名，首期至少包含 `cdn.usrgrow.com`。

### API 兼容

`GET /api/public/tt-drama/resolve?content_id=<id>` 的参数、HTTP 状态、`found/data` 包装、`Cache-Control: no-store`、`X-TT-Drama-Cache` 和 `Server-Timing` 保持兼容。

公开 `data` 继续提供：

- `content_id`
- `title`
- `description`
- `cover_url`
- `country`
- `language`
- `episode_count`
- `source_updated_at`

W2A 源不提供的兼容字段使用安全默认值：`country=""`、`language=""`、`episode_count=0`，`source_updated_at` 使用成功抓取时间。源 URL 仅在 HTTP 客户端内临时构造，不写入 SQLite 或公开响应；租约和错误信息也不对外公开。

公开 `X-TT-Drama-Cache` 保持旧接口语义：内部 `ORIGIN_FILL` 和 `NEGATIVE_FILL` 映射为 `MISS`，内部 `DISK_HIT` 映射为 `HIT`；内部存储实现状态不得直接扩大公开枚举。

主 API 的 systemd drop-in 不设置全局 `UMask`，避免改变单体后台其他功能创建文件时的权限。state 目录仍必须通过固定 `install -d -m 2770 -o tt-drama-featured -g tt-drama-featured` 创建；缓存模块仅对 `resources.sqlite3`、`resources.sqlite3-wal`、`resources.sqlite3-shm` 显式规范为 mode `0660`，无法检查或规范时 fail closed，从而在不扩大整个进程权限影响面的前提下支持 API 与离线任务共享 SQLite。

## 异常与边界

- 返回 HTTP 200 但实际 ID 不一致：404，可写短期负缓存。
- 深链缺失、重复或不可解析：503，不写负缓存。
- 标题或封面缺失：503；有旧正缓存时返回 `STALE`。
- `.info .desc` 元素缺失：503，不写负缓存；元素存在但文本为空：允许返回空字符串。
- 封面 URL 非 HTTPS、含账号信息、端口异常或不在白名单：视为资源解析失败。
- HTML 中的文本按纯文本输出，解码实体、压缩空白并限制长度；前端继续使用 `textContent`。
- 同一 ID 的 API 与预热并发只能有一个回源 leader。
- 数据盘未正确挂载、UUID 不符、空间不足、路径为软链接或 SQLite 不可写时 fail closed；初始化后父目录 `st_dev`、目录身份或路径软链接状态发生变化时，每次连接前后同样 fail closed。
- 预热候选源故障不影响公开 API 的已有正缓存；不得清空缓存。
- 非 `2049` 的 W2A landing 配置属于装配错误：`app.py` 记录错误并回退旧 MySQL resolver，以保护单体 API 可用性；该回退不代表新功能配置有效。

## 验收标准

- `Ag0rfr5F0F` 能从原始 HTML 提取 `Her Beast`、对应描述和 `cdn.usrgrow.com` 封面。
- 抓取过程没有浏览器进程，也没有 CSS、JS、图片、视频、Pixel 或 OneLink 请求。
- 合法格式但不存在或被 W2A 回退的 ID 返回 404，页面没有可点击 CTA。
- 源结构损坏、超时或 5xx 返回 503，不产生负缓存；已有合格旧值时返回 `STALE`。
- 正缓存、负缓存、进程重启持久命中、租约过期接管和并发 single-flight 测试通过。
- 首次完整 mount/UUID 校验记录父目录设备号；模拟数据盘运行中丢失、父目录设备号变化或路径被替换为软链接时，下一次 connect 前或后必须拒绝并且不在系统盘创建数据库。
- resolver 成功响应字段与现有客户端兼容，追踪参数透传行为不变。
- resolver 公开缓存头只使用旧语义枚举；内部 `ORIGIN_FILL/DISK_HIT/NEGATIVE_FILL` 不直接泄露。
- 预热候选严格限定最近 3 个上海自然日、固定 insight 表/索引和 Dramawave W2A 条件；普通单轮硬上限 500，只有显式 bootstrap 可到 3000。
- cursor v2 保持花费排名顺序；候选变化时优先从 `next_content_id` 接续，ID 消失时由 `next_index` 兜底。失败项进入有界重试队列且不会挤占全部正常轮转位置；显式 bootstrap 从最高花费候选开始。
- timer 每 4 小时的第 20 分钟触发，手工和定时运行均无任务重叠。
- featured 排名不变、资源来自共享缓存、失败不覆盖上一份完整快照。
- 缓存命中不访问 W2A；生产缓存命中 API P95 小于 50 ms。
- 所有发布文件来自已推送的 GitHub 精确 commit，具备可验证回滚点。
- 生产预检必须确认 `TT_DRAMA_RESOURCE_SOURCE=w2a_cache` 且 `TT_DRAMA_RESOURCE_LANDING_ID=2049`；任一不满足都应阻止新功能发布，不能把运行时 MySQL 安全回退当作验收通过。

## 生产验收记录

以上验收标准已通过：

- 生产 commit：`e77dba9c5d742e5e982c3faa44e9303761f0ff0b`
- Release：`/mnt/data-disk/tt-drama-resource-cache/releases/ai-tt-w2a-cache-e77dba9c5d74`
- 生产 app SHA-256：`ac68d0cc7c4b58ce9a242b6c12d6b45391b57f847a5e0801aff30d6f69310398`
- 有效、错误、短 ID API canary、30 次缓存性能、featured 5 项、500 部预热、timer、跨用户 SQLite/WAL 权限与真实浏览器均通过；详细数值见 `test-report.md`
- 回滚点：`20260727T092255Z-predeploy`、`20260727T094144Z-concurrent-x-baseline`

## 风险与决策

- W2A HTML 结构可能调整：parser 版本化，结构异常返回 503，并通过测试样本和任务日志暴露。
- W2A 可能限流：预热默认单轮 500、低并发、限速并使用缓存跳过新鲜记录。
- SQLite 同时被 API 和 oneshot 写入：通过 WAL、busy timeout、短事务和跨进程租约控制。
- 生产根盘空间紧张：缓存、release 和备份均必须位于已验证的数据盘。
- 长驻 API 运行中数据盘可能掉载：首次校验后锁定父目录 `st_dev`，每次 connect 前后复查路径和设备身份，变化立即 fail closed。

## 变更记录

- 2026-07-27：方案确认，资源源改为 W2A 原始 HTML GET，新增 SQLite 持久缓存和最近 3 日投放剧预热。
- 2026-07-27：实现、GitHub-first release 与生产验收完成；复合主键、无源 URL 持久化、公开缓存状态兼容映射、cursor v2 有界重试、固定 insight 表/索引、state `2770` 与 SQLite 文件 `0660`、connect 前后 `st_dev` 复查、无主 API 全局 UMask、装配错误安全回退，以及 featured 无剧库元数据查询均已验证。
