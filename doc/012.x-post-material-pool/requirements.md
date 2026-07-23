# 012.x-post-material-pool 需求与技术设计

## 背景

现有 X 每日发布按前一天 `ads_custom_source_insight` 消耗排名自动选材。新需求改为由管理员在 AI 后台录入自定义素材库 ID，三个固定 X 账号每天从一个全局素材池中按先进先出顺序各取一条安全素材发布，并延续已有短链、发布日志、全局排重和失败审计能力。

## 目标

- 在 AI 后台提供管理员专用的“Post 素材池”页面，可批量录入 `ads_custom_source.id`。
- 素材池全局共享，不按账号拆池；定时任务按 `created_at ASC, id ASC` 选择最早的合格素材。
- 素材池主状态只保存 `unpublished`、`published`，其他运行态从 queue/log 派生。
- 任何素材一旦进入任意 X 发布队列即永久占用，不能删除、重新入池或自动换账号补发。
- 只有 X 明确返回成功且本地发布日志成功落库时，素材池主状态才变为 `published`。
- 空池或不足三条合格素材时，整批不创建发布计划、三个账号均不发布。

## 范围

### 包含

- 新增 `x_post_material_pool` SQLite 表及 queue 关联字段、索引、触发器。
- 素材池管理页面、管理员查询/添加/删除 API、素材源文件预览和后台审计日志。
- daily runner 从素材池读取候选并回写素材校验结果。
- 从 `ads_custom_source` 直接按 ID 加载素材，不再用前一天消耗作为排序或入选条件。
- 保留 Dramawave 产品、素材类型、删除态、时长、HTTPS、剧映射、违规记录、色情/暴力标签和媒体文件预检。
- queue 创建时的全局素材排重、账号日排重、三条计划原子提交。
- 已发布、已占用、已失败、结果待核查和校验失败的后台展示。

### 不包含

- 不改变三个固定 X 账号、每日调度时间、账号 Token/OAuth、发布文案和 W2A/短链格式。
- 不允许管理员直接修改池主状态，也不提供失败后自动释放或自动重发。
- 不按账号维护独立素材池，不提供素材优先级插队。
- 本次上线允许部署生产与恢复既有 timer，但不手工触发 daily service、不手工创建计划、不额外调用真实 X 发帖。

## 用户故事 / 业务规则

1. 管理员可一次录入 1 至 100 个正整数素材 ID；前导零统一规范为十进制 `material_key`。
2. 同一批次重复、已在池中、或已有任意 X queue 历史的素材，整批添加失败，不做部分写入。
3. 录入只建立待选记录，不代表素材已通过安全检查。
4. 正式日更只接受 `ads_custom_source.product = 'Dramawave'`、`type = 2`、`is_delete = 0`、视频时长 1 至 140 秒的素材。
5. 素材必须有 HTTPS URL、完整名称/语言/content ID，并能唯一解析到同 content ID、同语言的短剧记录。
6. Facebook、TikTok、Twitter 违规表和资源审核记录必须全部为 0。
7. `ads_custom_source.tag_name`、`resource_tags.tag_name` 和短剧 labels 均执行色情、暴力等危险词检查；任一命中即跳过。
8. 选择顺序仅由池记录的 `created_at`、`id` 决定；`source_date` 仍记录为运行日前一天，但不再查询 `ads_custom_source_insight`，候选 `spend` 固定为 0。
9. 数据质量或安全不通过是单素材拒绝，可继续扫描后续素材；MySQL 查询异常是批次异常，整批停止。
10. 下载、大小、编码、时长、分辨率等媒体预检在建计划前执行；可用后续候选补位。
11. 只有恰好三条候选通过全部检查，才以一个 SQLite 事务建立一条 run 和三条 queue。
12. queue 以 `pool_item_id` 和规范 `material_key` 双重关联素材池。任何同素材 queue 历史都视为永久占用。
13. known failure 保持池主状态 `unpublished`，派生状态为 `failed`；unknown 或残留 `post_creating` 派生为 `needs_review`，两者均不能再次选择。
14. X 明确成功后，queue、publish log 和 pool 在同一事务中更新；池主状态变为 `published` 并记录 `published_at`。
15. 只有未发布且不存在任何同池 ID/同素材 key queue 的记录可以删除；已发布和已占用记录必须保留审计。
16. runner 按 `X_POST_DAILY_SCAN_LIMIT` 读取最老的池记录，默认和当前生产建议均为 1000、允许 3 至 1000；selector 再按 FIFO 保留最多 `X_POST_DAILY_CANDIDATE_POOL_LIMIT=50` 条合规候选供媒体预检补位。最老 1000 条内不足三条合格素材时整批不发布。
17. selector/媒体拒绝结果按 Sidecar 单次上限 100 条分批回写；例如 205 条必须按 100/100/5 三批提交，避免整批审计丢失。
18. 素材池明细将“素材预览”和“Post 预览”分列展示。管理员点击素材预览时，主后台先确认素材仍在池中，再按素材 ID 只读解析 `ads_custom_source.url`；仅允许无凭据、无控制字符的 HTTPS URL，并以 no-store 的 302 跳转打开。不存在、重复、非法或非 HTTPS 的源 URL 一律显示为不可预览，且该操作不修改池、queue 或发布日志。

## 交互与流程

1. 管理员进入“X 平台 > Post 素材池”，粘贴素材 ID 并提交；可在明细点击“预览素材”核对自定义素材库源文件。
2. 主后台验证 Cookie 管理员和同源 JSON，将 actor 与素材 ID 转发给 loopback Sidecar。
3. Sidecar 在一个事务中完成规范化、池内排重、历史 queue 排重和入池。
4. daily runner 先验证存储和三个固定账号，再取得最老的未发布且未占用素材。
5. selector 直接读取 `ads_custom_source` 与安全/剧映射表，返回合格候选和逐素材拒绝原因。
6. runner 回写校验结果，下载并预检媒体；不足三条时只记录 `failed_preflight` run，不创建 queue。
7. 三条均通过后，Sidecar 在一个事务中再次校验 FIFO、池快照、全局排重和账号日排重，再冻结三条 queue。
8. runner 按账号顺序发布；成功、known failure、unknown 分别写入 publish log，池状态按本需求规则派生或转换。

## 技术设计

### 影响模块

| 模块 | 变更 |
| --- | --- |
| `features/x_posts/service.py` | 素材池 schema、事务、FIFO、排重、派生查询、成功态联动 |
| `features/x_posts/selector.py` | 手工池素材加载与合规/映射校验，不使用 insight 排名 |
| `scripts/x_post_daily_runner.py` | 读取池、回写检查、三条成组预检与计划 |
| `features/x_accounts/oauth_service.py` | backend/daily 两种 bearer 的素材池内部路由 |
| `features/x_accounts/client.py` | 主后台到 Sidecar 的管理客户端 |
| `app.py` | Cookie 管理员 API、同源写校验、素材池范围内的安全源 URL 跳转、审计日志 |
| `static/x-post-material-pool.html` | 批量添加、筛选、分页、状态、素材/Post 双预览和受限删除 |
| `static/navigation.json` / `static/quick-nav.js` | 管理员导航入口 |
| `.env.example` / `deploy/x-post-daily.env.example` | daily 素材池可用项与检查回写路径 |

### 数据结构

`x_post_material_pool`：

| 字段 | 规则 |
| --- | --- |
| `id` | SQLite 自增主键，也是相同时间下的 FIFO 次序 |
| `material_key` | 规范正整数文本，全局唯一 |
| `material_id` | 展示/追踪素材 ID，与 `material_key` 一致 |
| `status` | 仅 `unpublished`、`published` |
| `published_at` | 仅 X 明确成功后写入 |
| `last_checked_at` | 最近一次未占用素材检查时间 |
| `last_error_code/message` | 最近一次校验失败，输出前脱敏 |
| `created_by_user_id/name` | 入池管理员审计字段 |
| `created_at/updated_at` | UTC 时间 |

`x_post_queue` 增量字段：

- `pool_item_id`：关联池主键，非空时必须与 queue 的 `material_key` 匹配。
- `pool_created_at`：冻结入池时间，计划提交时校验快照和 FIFO。
- 唯一索引 `ux_x_post_queue_pool_item_id` 保证池记录只绑定一条 queue。
- 既有 `ux_x_post_queue_material_key` 继续保证任意素材全局只进入一条 queue。
- 触发器同时防止无效绑定、池中素材被非池 queue 绕过、以及删除已占用池记录。

派生可用态：

| 池主状态 / 关联日志 | `availability` |
| --- | --- |
| `published` | `published` |
| 未发布、无 queue、无检查错误 | `available` |
| 未发布、无 queue、有检查错误 | `validation_failed` |
| 未发布、有关联 queue，known failure | `failed` |
| 未发布、有关联 queue，unknown 或 `post_creating` | `needs_review` |
| 未发布、有关联 queue，其他非终态 | `occupied` |

### API / 接口

- `GET /api/admin/x-posts/material-pool`
- `GET /api/admin/x-posts/material-pool/preview?material_id=...`
- `POST /api/admin/x-posts/material-pool`
- `DELETE /api/admin/x-posts/material-pool/{pool_item_id}`
- `POST /internal/posts/material-pool/query`
- `POST /internal/posts/material-pool/add`
- `POST /internal/posts/material-pool/{pool_item_id}/delete`
- `POST /internal/posts/material-pool/available`
- `POST /internal/posts/material-pool/check`
- `POST /internal/posts/daily-plan`：daily bearer 调用时强制三条候选都绑定素材池。

详细请求/响应见 `api-doc.md`。

### 异常与边界

- 管理页面只接受 Feishu Cookie 管理员；API Token、普通用户和跨源写请求拒绝。
- daily bearer 只能读取可用项、回写检查、创建固定三账号计划和发布正式 queue，不能管理池或查询后台列表。
- 添加、计划和成功态联动均使用 `BEGIN IMMEDIATE`，冲突全部回滚。
- 校验失败记录仍可在后续运行重新检查，但只要已有任何 queue，就永不回到可选择集合。
- 失败/unknown 不能靠删除池记录或重新入池绕过。
- 查询返回脱敏错误，不返回 OAuth Token、内部 bearer 或数据库凭据；响应使用 `Cache-Control: no-store`。
- 素材预览只接受池内 1 至 19 位正整数 ID，源地址只允许安全 HTTPS；跳转响应不缓存、不发送来源页，预览失败不回退到任意外部 URL。

## 验收标准

- [x] 管理员可批量添加规范素材 ID，重复/历史占用时整批回滚。
- [x] 非管理员、API Token、跨源写请求均无法管理素材池。
- [x] 正式选择路径不查询 `ads_custom_source_insight`，不使用 spend 排序。
- [x] 只有 Dramawave 的有效视频素材可以进入候选。
- [x] 四类违规证据、三处危险标签、剧映射和媒体预检全部 fail closed。
- [x] 合格素材严格按 `created_at,id` 顺序进入三个固定账号队列。
- [x] 少于三条合格素材时 run 记录失败原因，但 queue、短链和 X Post 均为 0。
- [x] queue 先于池、池先于 queue 两个方向都无法绕过全局排重。
- [x] known failure/unknown 的池主状态仍为 `unpublished`，但派生为不可重发。
- [x] 只有确认成功的三方结果才把对应池记录改为 `published`。
- [x] 已发布或任意 queue 占用素材不可删除；未占用未发布素材可删除。
- [x] 管理页状态、筛选、分页、预览 URL allowlist 和 no-store 契约通过。
- [x] 素材预览仅对管理员开放、仅解析池内素材的安全 HTTPS URL，并与 Post 预览分列。
- [x] 全部 X 回归与新增素材池测试 141/141 通过。
- [x] 生产副本迁移、live composite、精确 release 部署与 timer 恢复通过。
- [ ] 首轮自然 timer 发布验收待 2026-07-24 10:00 CST；素材池不足三条时应整批不发。

## 风险与部署待验证

- `ads_custom_source.product = 'Dramawave'` 已同时在 SQL 与行级校验中 fail closed，并有其他产品负例；生产只读 schema/数据抽样已确认。
- `summary.available` 已排除 `last_error_code`，专项测试验证不再把 `validation_failed` 计为可用。
- 单次安全扫描明确限制为最老 1000 条，而非无界遍历全池；如果这 1000 条长期不足三条合格素材，整批不发并由管理员修复元数据或删除仍未占用的无效池记录。
- 检查回写已按 100 条分批；205 条 100/100/5 回归通过。Sidecar 整体不可用时仍保持 best effort，不影响发布排重和主状态。
- 生产 SQLite 的 legacy canary/queue 已在副本与正式迁移中验证兼容，原 queue/log 各 1 条均保留。
- 生产主后台 composite 已核对并保留公网 `quick-nav.js`；只结构化修改 `navigation.json` 的 X 模块。
- 素材池上线后初始为 0 条。下一次自然触发前若未录入至少三条合格素材，三个账号均不会发帖。

## 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-07-23 | 按人工全局素材池、FIFO、永久排重和成功后发布态建立需求与技术设计 |
| 2026-07-23 | 精确 commit `75f46e7` 部署生产；素材池初始为空，恢复次日 10:00 自然调度 |
| 2026-07-23 | 素材池明细新增独立“素材预览”列，通过管理员池范围校验后安全跳转自定义素材库 HTTPS URL |
| 2026-07-23 | 精确 commit `9711ef77809e53ec4159b0b7f8bd6fe86fdc23d4` 的主后台接口与素材池页面部署生产；浏览器实测有效素材 302、缺失素材 404，发布账本零变化 |
