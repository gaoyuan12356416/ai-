# 008.x-post-daily-scheduler 需求与技术设计

## 背景

单条 X Post 灰度已在账号 `ShortsDramhx` 上成功完成，短链、W2A 跳转、视频上传、Create Post 和发布日志均已验证。现需将该能力扩展为三个已授权账号每天各发布一条，并在 AI 后台提供可审计的发布日志表。

## 目标

- 每个北京时间自然日，由固定三个账号各发布一条 Dramawave Post。
- 素材来自前一日 `ads_custom_source_insight`，按素材总消耗降序筛选。
- 排除违规记录、色情/暴力危险标签、剧信息不完整或多义、媒体技术规格不合格的素材。
- 同一素材跨账号、跨日期永久不重复；同一账号同一天最多一条。
- 发布正文保持 `{short_url}\n{desc}`，短链再跳转到固定 Dramawave W2A 长链。
- AI 后台提供管理员可见的发布批次和发布日志表。
- 任务可审计、可恢复、可回滚，且不向日志或后台暴露 OAuth/数据库敏感值。

## 范围

### 包含

- 全局素材排重、账号日排重、每日批次状态。
- 前一日 Dramawave 素材只读筛选与合规/媒体预检。
- 三个固定有效 X 账号顺序发布。
- systemd oneshot + timer，每天 `10:00 Asia/Shanghai` 执行。
- 管理员发布日志 API、页面、筛选和预览链接。
- SQLite 增量迁移、生产备份、GitHub-first 部署和回滚。

### 不包含

- 付费 X Ads、Post 推广投放或广告账户操作。
- 自动删除/修改已发布 Post。
- 对 `unknown` 结果自动重试、换账号或换素材补发。
- 允许后台用户手工绕过合规或排重门禁。
- 修改 X OAuth 授权、软停用和 Token 生命周期语义。

## 用户故事 / 业务规则

1. 定时任务使用北京时间当天作为 `run_date`，前一天作为 `source_date`。
2. 三个账号由 root-owned `0400` 配置 `X_POST_DAILY_ACCOUNT_IDS` 明确指定；必须恰好三个不同正整数账号，且运行时均为 `active + publish_eligible`，否则本批次不创建队列。
3. 候选按 `SUM(spend) DESC, material_id ASC` 稳定排序，最多读取配置上限内的候选。
4. 素材必须为 HTTPS 视频、未删除、时长 `1..140` 秒；实际下载与 ffprobe 必须通过现有 X 视频门禁。
5. `ads_facebook_violations`、`ads_tiktok_violations`、`ads_twitter_violations` 或 `ads_resource_audit` 任一命中即排除。
6. `resource_tags.tag_name`、`ads_custom_source.tag_name`、`ads_drama_resource.labels` 任一中英文色情/暴力危险词命中即排除。
7. content_id、语言、剧名、描述或业务标签缺失/多义时 fail closed。
8. 只有先找到三个不同且技术预检通过的素材，才用一个 SQLite 事务创建批次和三条队列。
9. `material_key` 为规范十进制素材 ID；它在全库唯一。凡已进入队列的素材，无论最终成功、失败或 unknown，均不再用于任何账号和日期。
10. `account_id + run_date` 全库唯一；一个账号一天最多占用一条。
11. 只有 queue=`queued` 且 publish log=`reserved`、`attempt_count=0` 时可在同一队列上安全恢复；进入 `media_uploading`、`post_creating`、`published`，或 `unknown_outcome=1` 后禁止自动重复 Create Post。
12. 三个账号顺序发布，不并发上传。单账号失败继续写日志；X 429、应用级限流或不确定结果会停止剩余批次，等待人工核查。
13. 日志只返回安全字段；Token、内部 bearer、数据库密码、OAuth code/state/verifier 不进入 API、DOM、journal 或 SQLite 错误文本。
14. timer 部署首日配置 `X_POST_DAILY_START_DATE` 为次日，避免 `Persistent=true` 在部署当天补跑。
15. 账号、数据源、合规或媒体预检在建计划前失败时，写入脱敏的 `failed_preflight` run，但不创建 queue、短链或 Post；正式计划一旦存在不得被失败记录覆盖。
16. 五类合规计数必须逐项显式提供且均为 0；缺字段、NULL、别名冲突一律拒绝，不能按 0 推断。
17. 建计划前必须确认 `/mnt/data-disk` 是真实挂载点，固定 `s2l`/`media-work` 目录均非符号链接、同盘、可原子写入且空间充足。
18. 预检视频 SHA-256/字节数写入 queue；正式发布重新下载后必须完全一致，否则按明确失败记录且不调用 X。
19. 短链基础地址固定为 `https://ai.yingliangads.com/s2l`；不接受其他 HTTPS 主机或路径。
20. 剧描述在 selector、queue 和 daily-plan 统一限制为最多 4096 个字符；daily-plan 使用独立 256 KiB UTF-8 JSON 硬上限。
21. daily runner 使用与后台管理令牌不同的专用 loopback bearer；该令牌不能访问 canary、authorize、通用 accounts verify/query/logout 或日志查询，只能访问固定日更路由。
22. Sidecar 对 daily bearer 强制配置中的三个账号，daily-plan 必须恰好覆盖三者；publish 只能处理三者所属且带 `run_id` 的正式日更 queue。
23. 短链临时文件必须先 `fchmod(0644)`、fsync 后再原子 replace，并 fsync `s2l` 目录；任一步失败都必须发生在 Create Post 前并记录 known failure。
24. daily-plan 响应必须回显同一 run ID、run/source date 和请求素材，三条 queue ID 和 account ID 均为互异正整数且账号顺序一致；响应丢失/畸形按 plan outcome unknown 人工核对。
25. plan/publish 写接口错误必须显式返回 `outcome_known/unknown_outcome`；缺失、矛盾或不可解析的 publish 响应一律 unknown。Create Post 非 JSON 5xx 同样 unknown。
26. X HTTP 429、官方 `usage-capped` / `rate-limit-exceeded` type 及 error code `88` 统一为 `x_post_rate_limited`，停止剩余账号并将 run 标记 `stopped`。
27. Sidecar 的 root-only EnvironmentFile 只由 systemd 读取并注入；降权进程遇 EACCES 安全跳过重读。所有 loopback bearer/readiness 客户端显式禁用环境代理。

## 交互与流程

```text
x-post-daily.timer
  -> x-post-daily.service
  -> 每日 runner 计算 run_date/source_date
  -> Sidecar 数据盘/短链目录原子写与空间门禁
  -> 专用 daily bearer + Sidecar 固定三账号范围校验
  -> 只读 MySQL 候选与合规筛选
  -> 下载 + ffprobe 预检，冻结 SHA-256/尺寸，凑齐 3 条
  -> SQLite 单事务创建 run + 3 条 queue
  -> 逐条调用 loopback Sidecar publish-by-queue
  -> 重新下载并核对指纹
  -> 账号锁内校验身份/刷新 Token/上传/Create Post
  -> 更新 run、queue、publish_log
  -> AI 后台管理员日志表查询
```

## 技术设计

### 影响模块

- `features/x_posts/service.py`：增量 schema、全局占用、批次、日志查询。
- `features/x_posts/selector.py`：只读 MySQL 候选与合规筛选。
- `features/x_accounts/oauth_service.py`：publish-by-queue、日志/批次内部查询。
- `features/x_accounts/client.py`、`app.py`：管理员日志只读转发。
- `scripts/x_post_daily_runner.py`：每日编排、预检与顺序发布。
- `static/x-post-logs.html`、`static/quick-nav.js`、`static/navigation.json`：后台日志页面与导航。
- `deploy/x-post-daily.service`、`deploy/x-post-daily.timer`、配置示例。

### 数据结构

- 新增 `x_post_daily_run`：`run_date` 唯一，记录目标数、队列数、成功/失败/unknown 数及开始/结束时间。
- `x_post_queue` 增量增加 `run_id`、`run_date`、`material_key`、`candidate_rank`、`spend`、媒体预检指纹与合规计数快照。
- 唯一索引：非空 `material_key` 全局唯一；非空 `account_id + run_date` 唯一。
- 旧 canary 行回填规范 `material_key` 和实际运行日期，使素材 `5221348` 进入永久排重。
- 保留 `x_post_publish_log` 作为唯一正式发布日志，不复制第二套含义相同的表。

### API / 接口

- Sidecar：`POST /internal/posts/queue/{queue_id}/publish`。
- Sidecar：`POST /internal/posts/accounts/{account_id}/verify`，仅供 daily bearer 校验配置中的三个账号。
- Sidecar：`POST /internal/posts/storage/preflight`，必须在 daily plan 前成功。
- Sidecar：`POST /internal/posts/logs/query`、`POST /internal/posts/runs/query`。
- AI 后台：`GET /api/admin/x-posts/logs`、`GET /api/admin/x-posts/runs`。
- AI 后台接口必须 Cookie 管理员鉴权、`Cache-Control: no-store`，API Token/普通用户拒绝。
- 日志筛选仅允许白名单字段，分页上限 100，SQL 全部参数化。

### 异常与边界

- 候选不足三个、合规证据不完整、账号不足三个、只读数据库异常、磁盘未挂载/不可写/空间不足、Sidecar health 异常：不创建正式队列或 Post。
- 旧库存在重复素材或同账号同日冲突时，迁移中止，不使用 `INSERT OR IGNORE` 掩盖。
- Create Post 网络断连、非 JSON 5xx、无效响应或结果形状不确定时记录 unknown，不自动重试；只有 Sidecar 显式标记 `outcome_known=true` 的结构化错误才按 known 处理。
- X HTTP 429 与官方应用级 `usage-capped`、`rate-limit-exceeded`、code 88 均按稳定 429 停止本批次。
- 进程在 X 返回后、数据库写 published 前中断时，`post_creating` 视为待人工确认。
- 明确失败的素材也保持占用，这是当前“发布素材永不重复”的最保守口径。

## 验收标准

- 三个配置账号每天各至多一条，队列与日志可按批次关联。
- 任意 `material_key` 在全库最多一行，旧 canary 素材不会再次入选。
- 四类违规、危险标签、剧映射和视频门禁均有自动化用例。
- timer 重启、Persistent 补跑和并发启动不会产生重复批次/队列/Post。
- 后台日志表可按日期、账号、状态、素材 ID 查询，并可安全打开短链/X 预览。
- 所有本地/服务器测试通过；DB、journal、DOM 的敏感字段检查为 0。
- 部署记录包含 commit、release、备份、timer 下一次触发时间和精确回滚步骤。

## 风险与待确认

- 用户未指定发布时间；本次采用可配置默认值 `10:00 Asia/Shanghai`。
- 生产主后台可能是多功能 composite，部署前必须用 live blob/hash 确认 GitHub 基线；不允许用旧分支覆盖当前 `app.py`。
- 素材预检与正式发布各下载一次视频，并以 SHA-256/尺寸强制一致，避免可变 URL 在两次下载间替换内容。

## 变更记录

- 2026-07-23：根据用户确认，将单条 canary 扩展为三账号每日正式任务，并要求 AI 后台日志表及个人 Skill。
> 2026-08-12 policy override: X publishing no longer queries or blocks on Facebook/TikTok/Twitter/resource-audit violation-history tables. Source/resource-tag, mapping, media, account, and unknown-outcome gates remain. See `doc/040.x-post-no-violation-manual-reuse/requirements.md`.
