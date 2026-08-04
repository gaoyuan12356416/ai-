# 031.TT 四位码搜索中间页需求与技术设计

## 文档状态

- 日期：2026-08-04
- 状态：需求已确认，代码与生产实测待完成
- 新公开页面：`https://ai.yingliangads.com/tt-code`
- 安全边界：本需求的开发、部署和验收不得创建或触发真实 TikTok Post

## 背景

现有 `https://ai.yingliangads.com/tt` 支持按 DramaWave `content_id` 搜索剧并跳转 W2A，但发布描述里缺少便于用户手输的短码，也没有把单次发布冻结的账号、素材、队列和归因字段作为一条可查询记录保存。现有 Featured stories 在部分触摸和桌面场景中缺少明确、稳定的横向浏览操作。

本需求新增一套独立页面和短码路由。原 `/tt` 页面、`static/tt-drama-search.html`、`static/tt-drama-search.js` 及原路由合同保持不变。

## 目标

- 新建 `/tt-code` 页面，支持四位发布 code 或完整 `content_id` 搜索。
- Featured stories 恰好展示五条，并支持触摸滑动、鼠标拖动和左右按钮浏览；横滑不得误触卡片跳转。
- 正式发布任务分配唯一四位 `A-Z0-9` code，并支持描述模板精确宏 `{code}`。
- 在数据盘 SQLite 中永久保存该发布的完整归因快照；Redis 只作为本机读缓存。
- code 搜索恢复该发布冻结的完整 W2A URL；直接剧 ID 和 Featured 点击按已确认的映射克隆规则生成目标。
- 保持发布重试幂等、并发唯一和既有 `{url}`、`{desc}`、Drama ID 宏行为。

## 范围

### 包含

- 新页面、新静态脚本和独立 Nginx exact route。
- 新公开解析接口 `GET /api/public/tt-code/resolve`。
- TT 发布 SQLite 加法表 `tt_post_code_route`、迁移、索引和事务分配。
- 四位 code 的生成、碰撞重试、全空间耗尽回收和 Redis 缓存失效。
- `{code}` 模板解析、预览、最终渲染、冻结和 UTF-16 长度校验。
- 正式 TT 发布完整归因 URL 的冻结。
- 直接剧 ID / Featured 的最新已发布映射克隆与无映射降级。
- Redis `127.0.0.1:6381` 的仅本机、仅缓存部署合同。
- 自动化、浏览器和生产只读验收；不触发真实发布。

### 不包含

- 不修改、替换或重定向现有 `/tt` 页面及其两个静态源文件。
- 不重写历史 queue、历史 caption、历史短链或既有 TikTok Post。
- 不让 Redis 成为事实源，也不因 Redis 故障阻断查询。
- 不新增 Instagram 发布流程；`Search`、`Featured` 是落地入口归因值，不是新的发布渠道。
- 不以真实 TikTok 发布、canary、`run-now` 或人工触发 scheduler 作为验收方式。

## 术语

| 名称 | 定义 |
| --- | --- |
| code | 四位大写 ASCII 字母或数字，正则 `^[A-Z0-9]{4}$` |
| code exact | 用户输入 code，按主键命中该 code 已冻结的归因快照，不受 queue 当前状态限制 |
| published clone | 按 `content_id` 找到同剧最新一条 published 路由，克隆参数后只替换 `af_channel` |
| generic fallback | 同剧没有 published 路由时沿用旧 `/tt` 的 `c=TTpost`、`af_c_id=0001`，再增加入口 channel |
| source | 新接口的精确枚举 `Search` 或 `Featured` |
| facts source | `/mnt/data-disk/tt-post-publisher/tt-post.sqlite3` 中的 `tt_post_code_route` |

## 用户故事与业务规则

### R1. 新旧页面隔离

1. 新页面 URL 固定为 `/tt-code`，返回 200、无重定向、`Cache-Control: no-store`。
2. 原 `/tt`、`tt-drama-search.html`、`tt-drama-search.js`、原 resolver 和原 Featured JSON 合同不变。
3. 部署前后必须对原两份静态文件和公网 `/tt` 做 SHA-256 / 行为回归；不得用新文件覆盖原文件。

### R2. Featured stories 横向浏览

1. 页面最终可交互列表必须恰好五条；动态 Featured 数据合法时使用动态五条，失败或过期时使用恰好五条安全 fallback。
2. 列表支持原生触摸横滑、鼠标按下拖动、键盘可聚焦的左右按钮；按钮按当前可视宽度滚动并正确处理首尾禁用状态。
3. 触摸或鼠标拖动超过点击阈值后必须抑制该手势产生的卡片 click；轻点仍正常解析并跳转。
4. 卡片使用 scroll snap，但不得锁死页面纵向滚动；尊重 `prefers-reduced-motion`。
5. Featured 点击调用新 resolver，`source=Featured`，只有解析成功且目标 URL 校验通过才导航。

### R3. code 字符集、唯一性和耗尽回收

1. code 字符集为大写 `A-Z` 与数字 `0-9`，固定四位，总空间 `36^4=1,679,616`。
2. 数据库存储必须为大写，`code TEXT PRIMARY KEY`；用户搜索输入可为大小写混合，后端统一转大写。
3. 正常分配使用系统安全随机源。随机碰撞由主键约束拦截，在同一写事务内安全重试；不得使用 `INSERT OR REPLACE` 静默覆盖已有记录。
4. 高占用时随机重试必须有界，并提供确定性查找剩余空位的兜底，避免接近满容量时无限循环或误报用满。
5. 只有确认所有 `1,679,616` 个组合均已占用时，才可在同一 `BEGIN IMMEDIATE` 事务内按 `created_at ASC, code ASC` 选择最早记录，删除旧映射并以同一 code 写入新发布映射。
6. 回收会使最早历史 code 指向新发布，这是产品已确认的容量耗尽行为；必须写入可审计事件，正常碰撞不得触发回收。
7. 同一 queue / 发布身份的幂等重试必须复用原 code，不得额外消费 code；并发请求最终只能保留一个 code。

### R4. code 生命周期与 `{code}` 宏

1. `{code}` 是精确小写、单花括号宏；`{CODE}`、`{{code}}`、`{ code }` 均拒绝。
2. 宏加入既有一次性、非递归 tokenizer；description 内出现 `{code}` 时保持普通文本，不得二次展开。
3. preview 可以显示明确的四位示例/待分配状态，但不得消耗生产 code。
4. queue 最终 caption 冻结前必须先在事务内分配并持久化 code，再渲染 `{code}`；最终 caption 不得残留宏。
5. 最终 caption 继续按 1..2200 UTF-16 code units 校验，不截断；分配或渲染失败不得创建可发布 queue。
6. 正式发布重试、未知结果保护和幂等回放均复用已冻结 code。精确 code 查询按主键命中所有已冻结状态，包括 `unknown`；不得因为尚未变为 `published` 而让可能已存在的帖子 code 失效。
7. 不含 `{code}` 的历史模板行为不变；是否为不含宏的正式发布也创建 code，以最终实现合同为准，但公共表、API 和测试必须保持一致并在代码评审中明确。推荐所有正式发布都创建 code，便于审计和按剧克隆。

### R5. 发布归因快照

1. `tt_post_code_route` 至少保存：
   - `code`（唯一主键）
   - `queue_id`（唯一发布身份）
   - `content_id`
   - `c`
   - `af_adset`
   - `af_adset_id`
   - `af_ad`
   - `af_ad_id`
   - `af_channel`
   - `af_c_id`
   - `long_url`
   - `state`、`created_at`、`published_at`、`updated_at`
2. 正式发布的 `af_channel` 固定为 `TT`。
3. `c` 固定格式：

   ```text
   yingliang_post_CLV_VL_用户名*时间戳none语言*剧名*标签*队列ID
   ```

4. `c` 最后一段是十进制 queue ID，不是四位 code、短链 ID 或 TikTok `publish_id`。
5. 其他字段固定映射：

   ```text
   af_adset=page名
   af_adset_id=page_id
   af_ad=素材名_contentid[短剧ID]
   af_ad_id=素材ID
   af_channel=TT
   af_c_id=队列ID
   af_dp=短剧ID
   ```

6. 最终地址基址固定为 `https://www.dramawavew2a.com/ads/101/2250/view`。参数按 `af_dp,c,af_adset,af_adset_id,af_ad,af_ad_id,af_channel,af_c_id` 顺序编码。
7. URL 必须用标准 query encoder 构造；`*` 可按现有业务合同保留，中文、空格、`&`、`#`、方括号等必须正确百分号编码，不得手工字符串拼接。
8. 用户名、page、素材、剧名、语言、标签、时间戳和 queue ID 都取发布时冻结快照；重试不得重新读取并漂移。

### R6. 搜索和目标选择

1. 新页面只调用：

   ```text
   GET /api/public/tt-code/resolve?query=<code-or-content_id>&source=Search|Featured
   ```

2. 四位字母数字输入按 code 处理并转大写；其他输入只能是现有 resolver 接受的完整 `content_id`，保留大小写精确匹配。
3. code 查询按主键命中精确记录，不以 `state` 过滤，返回其冻结 `long_url`，`af_channel` 保持 `TT`；`source` 不得覆盖它，也不得向公网暴露内部状态。
4. 直接剧 ID 搜索按 `published_at DESC, queue_id DESC` 找同剧最新 published 记录，克隆所有归因字段，仅把 `af_channel` 改为 `Search`。
5. Featured 点击使用同一最新 published 选择规则，仅把 `af_channel` 改为 `Featured`。
6. 直接剧 ID / Featured 没有 published 记录时，目标为：

   ```text
   https://www.dramawavew2a.com/ads/101/2250/view?af_dp=<content_id>&c=TTpost&af_c_id=0001&af_channel=Search|Featured
   ```

7. clone 和 fallback 不创建新 code、不修改原 published 记录，也不改变原 `c`、`af_c_id`、queue ID。
8. 必须先通过现有 DramaWave resolver 确认剧存在并取得标题、封面等公开元数据；数据库/上游异常返回 503，未找到才返回 404。只有 `found=true` 且 `target_url` 通过 host/path/`af_dp` 校验时，前端才显示可导航 CTA。

### R7. SQLite 与 Redis

1. SQLite 是唯一事实源，生产路径固定在数据盘 `/mnt/data-disk/tt-post-publisher/tt-post.sqlite3`；不得落到根盘或内存数据库。
2. `tt_post_code_route` 必须通过加法、幂等迁移创建，不删除或重建现有 queue / pool / event 表。
3. Redis 仅监听 `127.0.0.1:6381`，使用 `TT_POST_CODE_REDIS_*` 配置；公网不得访问。
4. Redis 只缓存公共查询所需的安全路由快照，不保存凭据，不承担持久化；SQLite 写成功不依赖 Redis 写成功。
5. 读路径为 Redis hit 优先，miss/超时/协议错误/连接失败立即回退 SQLite，并可在成功读取后回填缓存；Redis 故障不得把存在记录误报为 404。
6. code 被回收替换后，不得返回旧缓存。删除/覆盖缓存失败时必须旋转版本化 namespace，并在新 namespace 生效、对应 key 从 SQLite 安全刷新前旁路 Redis；需用自动化模拟陈旧缓存、`DEL` 失败、namespace 旋转与 Redis 故障。
7. 缓存 key 必须带版本化 namespace；正缓存和负缓存 TTL 可配置，负缓存必须显著短于正缓存。
8. API 可通过安全响应头报告 `HIT`、`MISS` 或 `BYPASS`，但不得把 Redis 地址、凭据或内部异常暴露给公网。

### R8. 安全和发布边界

1. 新公开接口沿用现有 TT resolver 的输入限制、token bucket、并发上限、超时和 `no-store` 策略。
2. 仅允许精确 GET；未知参数、重复关键参数、非法 `source`、非法 code/content ID 均 400。
3. `target_url` 必须是 HTTPS、host 精确 `www.dramawavew2a.com`、path 精确 `/ads/101/2250/view`、无用户信息/自定义端口，并且 `af_dp` 与解析剧 ID 一致。
4. 测试必须使用临时 SQLite、fake Redis、fake resolver 和隔离浏览器数据。不得调用 TikTok publish/canary、不得人工触发 `run-now` 或 scheduler。

## 技术设计

### 影响模块

| 模块 | 预计变更 |
| --- | --- |
| `features/tt_posts/code_routes.py` | 加法表、code allocator、公开路由读取和 Redis 缓存 |
| `features/tt_posts/core.py` | queue 事务接入、幂等与 `{code}` 冻结 |
| `features/tt_posts/links.py` | 正式 TT URL、published clone、fallback 和目标校验 |
| `features/tt_posts/service.py` | 发布快照落表、Redis 读缓存、内部查询能力 |
| `app.py` | 新公开 resolver、现有剧元数据合并、限流和错误映射 |
| `static/tt-drama-code-search.html` | 新独立页面与横向控件 |
| `static/tt-drama-code-search.js` | code/content ID 搜索、Featured 手势和安全导航 |
| `deploy/nginx/tt-drama-code-search.conf` | 新 `/tt-code`、静态脚本与公开 API exact route |
| `deploy/tt-post*.env.example` | `TT_POST_CODE_REDIS_*` 非敏感占位和缓存参数 |
| `scripts/test_tt_*` | 核心、服务、应用、页面和 bridge 回归 |

### 数据结构

建议加法表合同：

```sql
CREATE TABLE IF NOT EXISTS tt_post_code_route (
  code TEXT PRIMARY KEY
    CHECK(length(code)=4 AND code NOT GLOB '*[^A-Z0-9]*'),
  queue_id INTEGER NOT NULL UNIQUE,
  content_id TEXT NOT NULL,
  c TEXT NOT NULL,
  af_adset TEXT NOT NULL,
  af_adset_id TEXT NOT NULL,
  af_ad TEXT NOT NULL,
  af_ad_id TEXT NOT NULL,
  af_channel TEXT NOT NULL CHECK(af_channel='TT'),
  af_c_id TEXT NOT NULL,
  long_url TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  published_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
```

`state` 镜像既有 queue 状态枚举。精确 code 查询不按 state 过滤；按 content ID / Featured 选“同剧最新”时只把精确 `published` 视为候选。实际实现可增加审计/缓存版本字段，但不得削弱主键、queue 唯一、channel、状态和时间排序合同。需建立 `content_id + state + published_at + queue_id` 的最新 published 查询索引。

### 状态流

```text
queue freeze
  -> BEGIN IMMEDIATE
  -> 幂等查 queue_id
  -> 分配或回收 code
  -> 写 reserved 路由并渲染最终 caption
  -> commit
  -> 正式发布结果可确认
  -> 同一行更新 published / published_at
  -> Redis 安全失效或回填
```

发布失败或结果未知不得把 code 分配给第二个 queue；耗尽回收只按 R3 的全空间条件执行。

### API

- 新增 `GET /api/public/tt-code/resolve`，详见 `api-doc.md`。
- 既有 `/api/public/tt-drama/resolve`、`/api/public/tt-drama/featured` 合同不变。
- 管理端 queue/list 响应可加法暴露 `code` 和 route status 供审计，不得暴露 Redis 或内部 token。

## 验收标准

1. `/tt-code` 独立可用；原 `/tt` 及旧两份静态文件 hash、HTTP 和浏览器行为不变。
2. Featured 动态和 fallback 均恰好五条；触摸、鼠标拖动、左右按钮和键盘可操作，拖动不误触跳转。
3. code 只含四位 `A-Z0-9`，并发、随机碰撞、幂等、高占用兜底和全容量最早回收测试通过。
4. `{code}` 精确宏一次渲染，preview 不消耗 code，queue caption 无残留宏且 UTF-16 边界正确。
5. 正式路由 `c` 尾部和 `af_c_id` 均为 queue ID，`af_channel=TT`；字段与编码逐项正确。
6. code exact、Search clone、Featured clone、两类 generic fallback 目标 URL 全部通过。
7. SQLite 位于已验证数据盘且 `integrity_check=ok`；Redis 6381 仅本机监听，停止/超时/陈旧缓存时查询仍由 SQLite 正确返回。
8. 生产部署按 GitHub exact commit，可回滚；DB、静态、Nginx、env、systemd 均有备份。
9. 全量 TT 与原 bridge 回归通过，测试期间无真实 TikTok publish 请求、无人工 scheduler/run-now。

## 风险与已确认决策

| 风险 | 决策 / 控制 |
| --- | --- |
| 四位空间最终耗尽 | 用户确认事务内删除最早记录并替换；仅全空间确认后执行并审计 |
| 历史 code 被回收后改指向 | 作为已确认产品行为记录，不得在普通碰撞时提前发生 |
| Redis 陈旧值在回收后误路由 | 缓存写失效即旁路，SQLite 始终为事实源，专项故障测试 |
| 同剧多条发布记录 | `published_at DESC, queue_id DESC` 确定性选择最新 |
| `/tt` 被新发布覆盖 | 新文件、新路由，部署前后 hash 保护 |
| 验收误触真实发布 | 全部使用隔离数据；生产只做 GET/只读和自然健康检查 |

## 变更记录

| 日期 | 版本 | 说明 |
| --- | --- | --- |
| 2026-08-04 | v1 | 固化 `/tt-code`、四位 code、映射克隆、Redis 6381、耗尽回收和零真实发布合同 |
