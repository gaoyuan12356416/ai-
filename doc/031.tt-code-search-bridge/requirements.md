# 031.TT 四位码搜索中间页需求与技术设计

## 文档状态

- 日期：2026-08-04
- 状态：需求、实现、最终回归、生产部署与只读线上验收均已完成
- 新公开页面：`https://ai.yingliangads.com/tt-code`
- 安全边界：开发、测试和上线验收不得创建或触发真实 TikTok Post

## 背景与目标

现有 `https://ai.yingliangads.com/tt` 只支持按 DramaWave `content_id` 搜索。新能力以独立 `/tt-code` 页面提供四位发布码和完整剧 ID 搜索，同时为正式 TT 发布冻结可追溯的 AppsFlyer 参数。原 `/tt`、`static/tt-drama-search.html`、`static/tt-drama-search.js` 和原 resolver 合同保持不变。

交付目标：

- `/tt-code` 支持四位 code 或完整 `content_id` 搜索。
- Featured stories 恰好展示五条，支持触摸横滑、鼠标/触控笔拖动、左右按钮和 scroll snap；拖动不得误触卡片。
- 每条新正式队列都分配唯一四位 `A-Z0-9` code；描述模板可使用精确宏 `{code}`。
- SQLite 永久保存发布时的完整归因快照；Redis 仅作本机读缓存。
- code 恢复该队列冻结的 TT URL；直接剧 ID/Featured 使用该剧最新 published 快照，若从未发布则使用通用参数。

## 范围

包含：新 HTML/JS、独立 Nginx exact locations、公共组合 resolver、bearer 保护的 sidecar resolver、加法 SQLite 表/字段/索引/触发器、code 分配与回收、Redis 6381 缓存、正式队列 URL 和 `{code}` 冻结、自动化与浏览器验证。

不包含：修改原 `/tt`，重写历史队列/caption/短链，新增 IG 发布渠道，或以 publish/canary/`run-now`/人工 scheduler 作为验收。

## 已确认业务规则

### R1. 新旧页面隔离

1. 新入口固定为 `/tt-code`，使用新 `tt-drama-code-search.html/js`。
2. `/tt` 和原两份静态文件不得修改；部署前后必须比较 hash 并做原页面回归。
3. `/tt-code`、新脚本和新 API 都是 exact GET location，响应 `Cache-Control: no-store`。

### R2. Featured stories

1. 可交互列表始终恰好五条。合法动态数据使用其五条；请求失败、过期或数量/schema 不合法时整体降级到五条本地 fallback。
2. 支持触摸原生横滑、鼠标/触控笔拖动、左右按钮和键盘焦点；卡片使用 scroll snap。
3. 位移超过阈值后抑制该手势产生的 click；纵向页面滚动不得被锁死，并尊重 `prefers-reduced-motion`。
4. Featured 点击使用同一个组合接口并发送 `source=Featured`；前端只在响应和目标 URL 均通过严格校验后导航。

### R3. code 唯一性、幂等与回收

1. code 固定四位，字符集为大写 `A-Z` 和数字 `0-9`，正则 `^[A-Z0-9]{4}$`，容量为 `36^4=1,679,616`。
2. `tt_post_code_route.code` 是主键，`queue_id` 也唯一。输入允许小写，查询前转大写；数据库只接受大写。
3. 新正式队列无论 caption 是否使用 `{code}`，都在 queue freeze 的 `BEGIN IMMEDIATE` 事务内生成并持久化 code。
4. 正常分配使用系统安全随机源并有界重试；高占用时通过一次 O(capacity) 占用位图确定性找到空槽。普通碰撞不得覆盖或回收历史记录。
5. 只有确认全部组合已占用时，才按 `created_at ASC, code ASC` 选择最早记录，在同一事务内删除旧映射、复用其 code、插入新映射，并写入 `tt_post_code_recycle_audit`。
6. 同一 queue/idempotency key 重试复用原 code；冲突事实返回错误，不额外消耗 code。

### R4. `{code}` 宏与兼容性

1. 只支持精确小写单花括号 `{code}`；非法变体由现有占位符校验拒绝。
2. 宏沿用一次性、非递归渲染；description 内的 `{code}` 不会二次展开。
3. 管理页 preview 使用固定示例 `A1B2`，不分配真实 code；最终正式 queue caption 使用该 queue 已冻结的 code。
4. 最终 caption 继续执行 1..2200 UTF-16 code units 校验，不截断；分配、路由或渲染任一步失败，queue/route 事务整体回滚。
5. `{code}` 只适用于自动/排期正式队列。直接测试明确返回 `tt_post_code_macro_queue_only`，不会为测试发布生成 code。
6. 历史无 code 的 pending queue 保留原 `AIpost` URL 行为；直接测试也保持 `AIpost`。本改动只把新正式队列设为 `TT`。

### R5. 发布归因快照

`tt_post_code_route` 保存 `code`、`queue_id`、`content_id`、`c`、`af_adset`、`af_adset_id`、`af_ad`、`af_ad_id`、`af_channel`、`af_c_id`、`long_url`、`state`、`created_at`、`published_at`、`updated_at`。

新正式队列的冻结参数为：

```text
c=yingliang_post_CLV_VL_用户名*时间戳none语言*剧名*标签*队列ID
af_adset=page名
af_adset_id=page_id
af_ad=素材名_contentid[短剧ID]
af_ad_id=素材ID
af_channel=TT
af_c_id=队列ID
af_dp=短剧ID
```

最终基址固定为 `https://www.dramawavew2a.com/ads/101/2250/view`。新正式 URL 参数顺序固定为 `af_dp,c,af_adset,af_adset_id,af_ad,af_ad_id,af_channel,af_c_id`。统一使用标准 query encoder；业务分隔 `*` 保留，其他特殊字符必须安全编码。

### R6. 搜索和跳转

新页面只发起一次组合请求：

```http
GET /api/public/tt-code/resolve?query=<code-or-content_id>&source=Search|Featured
```

规则：

1. 四位 ASCII 字母数字按 code 处理并转大写；其他输入必须满足现有完整 `content_id` 规则（10..32 位 `[A-Za-z0-9_-]`），保持大小写。
2. code exact 按主键读取，不按 state 过滤，返回该发布冻结 URL，`af_channel` 保持 `TT`。若对应剧已无法由现有 DramaWave resolver 确认，公共接口仍 fail closed 返回 404。
3. 直接剧 ID/Featured 只从 `state='published'` 中按 `published_at DESC, created_at DESC, queue_id DESC` 取最新一条，克隆冻结参数，仅把 `af_channel` 改为 `Search` 或 `Featured`。查询不写库、不生成 code。
4. 若该剧没有 published 快照，使用：

   ```text
   https://www.dramawavew2a.com/ads/101/2250/view?af_dp=<content_id>&c=TTpost&af_c_id=0001&af_channel=Search|Featured
   ```

5. 无论 clone 还是 fallback，主 app 都必须通过现有 DramaWave resolver 确认剧存在并返回公开标题、封面、描述等元数据；未找到返回 404，上游不可用返回 503。

### R7. 公共链路和安全边界

```text
浏览器
  -> Nginx exact /api/public/tt-code/resolve
  -> 主 app 127.0.0.1:8787
     - 参数校验、现有 token bucket、并发 gate
     - bearer 调用 sidecar /internal/tt-posts/code-resolve
     - 现有 DramaWave resolver 剧目存在性/元数据校验
     - 校验并合并 route + drama 为一个 item
  -> 浏览器一次响应
```

sidecar 只监听 loopback，其 `/internal/tt-posts/code-resolve` 不是公开接口，必须携带现有内部 bearer。公共返回前再次验证 HTTPS、精确 host/path、无端口/userinfo/fragment、参数集合和 `af_dp`/channel 一致性。

公共接口沿用既有 token bucket 与 in-flight gate。响应 `no-store`，可返回现有 `X-TT-Drama-Cache` 与 `Server-Timing`；当前实现没有 `X-TT-Code-Cache`。

### R8. SQLite 与 Redis

1. SQLite 是唯一事实源，生产路径为 `/mnt/data-disk/tt-post-publisher/tt-post.sqlite3`。
2. 迁移是加法、幂等且事务化的：创建 `tt_post_code_route`、`tt_post_code_recycle_audit`、索引与状态同步 trigger，并给 queue 加法增加 `code` 字段；不删除旧表和历史行。
3. Redis 配置只接受 loopback，生产目标 `127.0.0.1:6381`。实际 env 只有 `TT_POST_CODE_REDIS_HOST`、`TT_POST_CODE_REDIS_PORT`、`TT_POST_CODE_REDIS_TIMEOUT_SECONDS`。
4. 正缓存 TTL 固定 24 小时，负缓存 TTL 固定 30 秒；namespace 每个进程随机生成。route 写入会先旋转 namespace，使所有旧 key 立即不可达；定向 code/latest 失效也先旋转，再在锁外 best-effort 删除对应旧 key。TTL/namespace 当前不是 env 配置项。
5. Redis miss、连接失败、超时、协议/JSON/字段校验失败都回退 SQLite；Redis 写失败不回滚 SQLite。缓存行必须完整匹配 code/content/state/冻结 URL 才可使用。

## 数据结构

核心加法表：

```sql
CREATE TABLE tt_post_code_route (
  code TEXT PRIMARY KEY,
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

CREATE TABLE tt_post_code_recycle_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  old_queue_id INTEGER NOT NULL,
  old_content_id TEXT NOT NULL,
  new_queue_id INTEGER NOT NULL,
  recycled_at TEXT NOT NULL
);
```

实际迁移还包括 code 大写/长度 CHECK、最新 published/最早记录索引，以及 queue 状态同步 trigger。

## 影响文件

| 层 | 文件 |
| --- | --- |
| 存储/业务 | `features/tt_posts/code_routes.py`, `core.py`, `links.py`, `service.py` |
| 公共组合接口 | `app.py` |
| 页面 | `static/tt-drama-code-search.html`, `static/tt-drama-code-search.js` |
| 部署 | `deploy/nginx/tt-drama-code-search.conf`, `deploy/tt-code-redis.conf`, `deploy/tt-code-redis-prepare.service`, `deploy/tt-code-redis.service`, env examples |
| 测试 | `scripts/test_tt_post_code_routes.py`, `scripts/test_tt_drama_code_bridge.js` 及既有 TT 回归 |

## 验收门禁

1. `/tt-code` 页面和一次组合请求工作正常，Featured 恰好五条且所有横滑方式不误触。
2. code 格式、PK/queue 唯一、碰撞、并发、幂等、确定性空槽与满池回收/审计测试通过。
3. 所有正式 queue 都有冻结 code；`{code}`、UTF-16 边界和直接测试拒绝规则通过。
4. 新正式 URL、code exact、Search/Featured clone 和两类 fallback 均通过字段、顺序和目标 allowlist 校验。
5. 迁移副本 `integrity_check=ok`；Redis 只监听 loopback且停止/超时时仍由 SQLite 正确查询。
6. 公共 API 的限流、并发门、bearer sidecar、剧目校验和安全错误响应通过。
7. 原 `/tt` 文件 hash 与行为不变。
8. 生产按 GitHub exact commit 部署，有 DB/静态/Nginx/env/systemd/旧 release 回滚点；验收不触发真实 TikTok publish。

## 风险与决策

| 风险 | 已确认控制 |
| --- | --- |
| 四位空间耗尽 | 仅确认全满后按 `created_at,code` 回收，写 recycle audit |
| 历史 code 被回收后改指向 | 产品已确认；普通碰撞严禁触发 |
| Redis 陈旧或不可用 | SQLite 为事实源；完整校验、namespace 旋转和自动降级 |
| 同剧多条发布 | `published_at,created_at,queue_id` 降序确定最新 |
| 旧流程归因漂移 | 新正式队列 TT；历史 pending 与直接测试继续 AIpost |
| 验收误发 | 仅隔离数据、GET/只读和自然健康检查，不调用发布入口 |

## 变更记录

| 日期 | 版本 | 说明 |
| --- | --- | --- |
| 2026-08-04 | v1 | 冻结产品需求 |
| 2026-08-04 | v2 | 按实际实现补齐主 app 组合链路、所有正式队列 code、兼容语义、回收审计和缓存固定参数 |
