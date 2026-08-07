# 035.tt-publish-logs 需求与技术设计

## 背景

TT Post 素材池页面当前同时承担素材入池、预制作和发布任务日志查看，页面过长；新自动发布模板又使用独立账本，运营人员无法在一个入口统一查看两类发布结果。

## 目标

- 将发布任务日志从 TT Post 发布池页面移出，提供独立“TT 发布日志”页面。
- 在同一任务级列表中展示素材池发布和自动模板发布，并通过稳定字段区分来源。
- 保留原素材池任务的事件、取消和人工核对能力，以及自动发布运行详情。
- 在列表和详情中展示账本冻结的 4 位 code，不能从发布文案推断。
- 为 code 路由上线前的已发布排期记录生成可解析 code，供运营手工修改帖子。
- 不迁移、不改写两套历史账本，不改变任何筛选、排期、准备或发布执行逻辑。

## 范围

### 包含

- 独立发布日志页面、导航入口、筛选、分页、统计和详情。
- 只读聚合 `tt-post.sqlite3` 与 `tt-auto-post.sqlite3` 的任务记录。
- `publish_source=material_pool|auto_template`。
- 独立 `trigger_type` 字段，避免将来源与触发方式混为一谈。
- 从素材池页面移除发布日志卡片、表格和相关主动查询。
- 一次性回填 `tt_post_queue` 中已发布、有 `publish_id`、但 `code` 为空的明确队列。

### 不包含

- 修改旧发布池和自动模板的发布状态机、定时器、幂等键或发布 API。
- 立即测试记录的 code 回填；`tt_post_direct_test` 仍无 durable code 身份。
- 修改历史 caption、长链、短链、状态、`publish_id` 或触发真实 TikTok 发布。
- 自动重试、批量取消、删除日志。

## 用户故事 / 业务规则

1. 运营人员进入“TT 发布日志”后可以统一查看全部发布任务。
2. `material_pool` 包含旧发布池的排期发布与立即测试。
3. `auto_template` 包含自动模板的定时执行与手动执行。
4. 来源中文标签固定为“素材池发布”“自动发布”。
5. 触发方式分别展示“排期发布”“立即测试”“自动定时”“手动执行”。
6. 任一来源不可用时聚合接口失败关闭，不返回可能误导的半份统计。
7. 自动发布任务无候选素材时仍属于日志，状态显示“无可投素材”。
8. code 只接受大写 `[A-Z0-9]{4}`；缺失或非法值显示 `—`。
9. 自动模板 code 按共享路由账本中的高位合成 queue ID 读取，不从 caption 提取。
10. 历史回填优先使用已冻结的 `AIpost` 长链；对从未生成长链的旧队列，只允许按逐 queue ID 显式授权，从同一 SQLite 快照中的唯一 `consumed` recurring 记录、唯一 `publish_reconciled → published` 事件和冻结账号快照生成确定性替代路由。缺少或冲突的账本证据必须整批失败关闭。
11. 确定性替代路由不能称为恢复原始长链：campaign 时间使用 `queue.created_at` surrogate，素材名/剧名/语言/标签缺失时分别使用 `material_id`、`content_id`、recurring `routing_language`、`none`，link identity 使用历史 `TT_SHORT_LINK_NAMESPACE + queue_id` surrogate。不得从 caption 或当前远端素材解析器推断历史字段。

## 交互与流程

1. 用户通过 TikTok 社媒导航进入 TT 发布日志。
2. 页面默认按任务时间倒序加载两类来源。
3. 用户可按来源、触发方式、账号、模板、素材 ID、Drama ID、统一状态和日期筛选。
4. 素材池排期任务可查看事件，并在原状态规则允许时取消或人工核对。
5. 素材池立即测试显示本地时间线；自动发布任务显示运行、任务和事件快照。

## 技术设计

### 影响模块

- `features/tt_auto_posts/legacy_reader.py`：只读查询旧任务。
- `features/tt_auto_posts/core.py`：只读查询自动发布任务。
- `features/tt_auto_posts/service.py`、`client.py`：统一日志接口。
- `app.py`：后台代理路由和权限映射。
- `static/tt-publish-logs.*`：新页面。
- `scripts/backfill_tt_published_codes.py`：默认 dry-run 的历史 code 回填工具。
- `static/tt-post-pool.html`：移除日志展示及主动加载。
- `static/quick-nav.js`、`static/navigation.json`：导航调整。

### 数据结构

不新增数据库字段或迁移。接口在读取时生成：

- `publish_source`: `material_pool` / `auto_template`
- `trigger_type`: `scheduled` / `direct_test` / `auto` / `manual`
- `status_group`: `scheduled` / `processing` / `published` / `needs_review` / `failed` / `canceled` / `no_candidate` / `hold` / `other`
- `task_key`: 带来源命名空间的稳定页面键
- `code`: 共享路由账本中的 4 位 code；无 durable code 时为空字符串

自动模板任务不新增镜像字段。统一读取模型将 `task_id` 转为既有高位合成 queue ID，仅从共享 `tt_post_code_route` 读取 code；共享路由不可用时自动任务日志仍可返回，但 code 留空。

### API / 接口

新增只读接口 `GET /api/admin/tt-auto-publish/publish-logs`。支持 `publish_source`、`trigger_type`、`source_account_id`、`template_id`、`material_id`、`content_id`、`status`、`from`、`to`、`limit`、`offset`。响应包含 `items`、`pagination`、`summary`。

### 异常与边界

- 分页单页最多 200 条，发布日志 offset 最大 10,000。
- 日期按 Asia/Shanghai 自然日转换为 UTC 半开区间。
- 两个来源分别取全局页末所需的前 N 条再合并排序，保证跨来源分页正确。
- 浏览器响应不得包含媒体源地址、凭据、claim token 或内部 token。
- 旧库始终以 SQLite `mode=ro` 与 `PRAGMA query_only=ON` 打开。
- 历史回填 apply 必须提供明确 queue ID、expected count、plan SHA-256 和全新备份路径；在 `BEGIN IMMEDIATE` 内分配全局唯一 code 并守卫更新。
- 空 `long_url` 的候选必须通过重复参数 `--reconstruct-route-from-ledger-queue-id` 逐 ID 授权，而且授权集合必须与所选候选中的空长链集合完全相等；不提供全局放行开关。
- 回填只写 `tt_post_code_route` 新行和 `tt_post_queue.code`。已有长链保留历史 `AIpost` 原值并仅在新 route 中改为 `af_channel=TT`；无长链记录不补写历史长短链、caption 或元数据。

## 验收标准

- TT Post 发布池不再显示或主动请求发布任务日志。
- TT 发布日志能同时出现两类来源，来源与触发方式标签正确。
- 过滤、统计、跨来源倒序分页准确。
- 旧历史记录无需迁移即可显示。
- 素材池取消/核对/事件以及自动运行详情仍可使用。
- 发布相关状态机、表结构和定时执行代码没有行为变更。
- 首批符合条件的已发布排期均有可解析 code，页面显示与路由账本一致；确定性替代路由带可审计 provenance/fallback，立即测试不在回填范围。
- 契约、服务、UI 和回归测试全部通过。

## 风险与待确认

- 已确认统一任务级展示；自动运行批次作为详情而不是独立来源。
- 发布来源为运行时派生字段，避免修改旧数据。

## 变更记录

- 2026-08-06：用户确认统一发布日志方案。
- 2026-08-07：用户要求恢复 4 位 code 列并为路由上线前的首批已发布排期生成 code。
