# 038.x-post-priority-manual-publish 需求与技术设计

## 背景

生产 X Post 自动化当前运行 GitHub 代码提交 `29bd900`。短剧池按“一剧一号”持续发布：已开始的短剧保持原账号直到免费集数完成，未分配短剧按入池倒序分配。素材池页面支持批量加入素材和自动排期，但没有不入池的人工立即发布批次。

本需求增加两项能力：

1. 在 Post 短剧池明细中对未分配短剧设置“高优”，使其在下一次新剧分配时优先。
2. 在 Post 素材池“加入素材池”按钮上方增加“手动立即发布”，把本批素材直接提交给指定 X 个号执行一次发布，不写入素材池。

## 目标

- 不改写短剧原始入池时间，提供可审计、可取消、并发安全的高优排序。
- 手动发布与自动排期解耦，目标账号和素材形成一批持久化任务。
- 手动素材不进入 `x_post_material_pool`，但在任何 X 写入前必须原子进入全局去重队列和发布日志。
- 完整复用 Dramawave、违规记录、危险标签、短剧映射、媒体、长视频会员资格、链接、文案和最终发布门禁。
- 浏览器请求快速返回，后台异步预检与串行发布；进程重启后可从持久化批次恢复。
- 部署验收不得创建真实 X Post。

## 范围

### 包含

- `x_post_drama_pool` 增量高优字段、排序索引和操作审计信息。
- 未分配短剧的高优/取消高优管理 API 和页面操作。
- `x_post_manual_run` 手动批次表、`x_post_queue.manual_run_id` 关联、唯一索引和完整性触发器。
- 手动批次创建、查询、领取、预检失败、原子建队列、恢复和发布聚合接口。
- 独立 `x-post-manual.service` + `x-post-manual.timer`，与自动排期共享发布锁。
- 素材页目标账号确认弹窗、二次真实发布警告、状态轮询和发布日志入口。
- 后端、Sidecar、runner、前端、迁移和回归测试。

### 不包含

- 不改变已绑定短剧的账号，不中断正在连载的短剧，不重写已冻结计划。
- 不允许手动发布已在素材池、任何队列或发布历史中的素材。
- 不提供失败素材自动释放、删除历史、自动重试或绕过人工核查。
- 不改变自动发布开关、随机/固定排期、当日计划和已保存文案模板。
- 不在部署或验收中点击手动发布，也不调用真实 X 写接口。

## 用户故事 / 业务规则

### 短剧高优

1. 页面仅对 `assigned_account_id=0`、状态为 `pending|active`、无校验错误且仍有免费集数的短剧开放“高优”。
2. 点击“高优”写入独立 `priority_at` 和操作人，不修改 `created_at`。
3. 多部高优短剧按最近高优时间倒序；同一短剧再次高优等价于移到高优队首。
4. 点击“取消高优”清空高优字段，恢复普通入池倒序。
5. 已绑定短剧仍由原账号优先续发；高优只影响没有未完成绑定的账号选择新剧。
6. 已冻结的日计划/时间点计划不重写；高优只影响下一次尚未建队列的选择。
7. 操作与调度并发时，Sidecar 在事务内复查状态；已分配、已完成、校验失败或状态变化返回 409。

### 手动立即发布

1. 操作人输入 1 至 50 个互不重复的正整数素材 ID，并在独立弹窗选择同样数量的目标账号。
2. 默认勾选已保存的素材池排期账号，但手动选择不保存、不修改自动排期草稿或服务端配置。
3. 服务端冻结精确账号 ID 顺序、素材 ID 顺序、当前已保存素材 Post 模板、操作人和客户端幂等键。
4. 每个目标账号只发布一篇；后台按当前 token 的 `subscription_type` 做账号感知匹配，优先把短视频留给普通账号，超过 140 秒的素材只能匹配当前合格会员账号。
5. 全批账号验证、素材源/合规解析、完整媒体下载/探测、必要 GPU 修复和文案检查全部成功后，才能在一个 SQLite 事务中创建全部队列。
6. 任一预检失败时整批 `failed_preflight`，不创建队列、不写 X；失败素材仍可在修正后重新提交，因为尚未进入全局队列。
7. 建队列事务必须再次拒绝：素材池已存在、任意历史队列已占用、账号/素材数量或快照变化、同批重复、幂等冲突。
8. 队列建立后素材永久受全局去重保护，即使明确失败、未知结果或未尝试兄弟队列也不得自动入池或重发。
9. 按冻结账号顺序串行发布。明确普通失败记录后继续下一账号；X 限流、未知结果或残留 `post_creating` 立即停止剩余队列。
10. X 写入不能事务回滚；页面必须在确认前明确提示可能出现部分成功。
11. 浏览器提交返回 `202` 和手动批次 ID，随后轮询状态。刷新或响应丢失时使用相同幂等键读取原批次，不重复创建。
12. 后台 worker 与正式排期共享 `/run/x-post-daily/runner.lock`，确保同一时刻只有一条生产发布链路占用账号/媒体资源。
13. 手动批次不受 `account_id + run_date` 的自动日更唯一约束，但自身必须满足 `manual_run_id + account_id` 唯一；这表示用户明确授权的手动 Post 可以与当天自动 Post 并存。

## 交互与流程

### 高优

1. 操作人打开短剧池明细。
2. 对可用未分配短剧点击“高优”，页面二次确认。
3. API 返回更新后的记录；列表把该剧显示为“高优”并置于未分配候选首位。
4. 点击“取消高优”恢复普通排序。

### 手动发布

1. 操作人在素材 ID 输入框粘贴本批 ID，点击位于“加入素材池”上方的“手动立即发布”。
2. 弹窗展示可发布账号，默认选中已保存排期账号，并实时校验素材数等于账号数。
3. 操作人确认真实发布风险后提交；页面显示批次 ID 和“等待预检”。
4. 手动 timer 领取批次，先恢复既有冻结队列；不存在队列时才重新验证账号、查询素材、下载/修复媒体并原子建队列。
5. worker 串行调用既有 Sidecar 发布门禁；页面轮询批次并提供发布日志链接。

## 技术设计

### 影响模块

| 模块 | 变更 |
| --- | --- |
| `features/x_posts/service.py` | 高优字段/排序、手动批次 schema、原子计划、恢复、聚合和查询 |
| `features/x_posts/selector.py` | 按明确素材 ID 顺序解析不入池候选 |
| `features/x_accounts/oauth_service.py` | backend/daily 角色的高优及手动批次内部接口 |
| `features/x_accounts/client.py` | 主后台高优、手动创建/查询客户端 |
| `scripts/x_post_manual_runner.py` | 持久化领取、预检、原子建队列和串行发布 |
| `scripts/x_post_daily_runner.py` | 允许手动候选使用独立修复身份，不放宽媒体门禁 |
| `app.py` | Cookie/导航授权、同源写保护、公开管理 API、审计日志 |
| `static/x-post-drama-pool.html` | 高优/取消高优按钮与徽标 |
| `static/x-post-material-pool.html` | 手动按钮、账号确认弹窗、幂等提交和状态轮询 |
| `deploy/x-post-manual.*` | 独立 oneshot/timer，复用排期环境和发布锁 |

### 数据结构

`x_post_drama_pool` 增量字段：

- `priority_at TEXT NOT NULL DEFAULT ''`
- `priority_by_user_id TEXT NOT NULL DEFAULT ''`
- `priority_by_name TEXT NOT NULL DEFAULT ''`

`x_post_manual_run`：

- 批次身份：`id`、唯一 `idempotency_key`、`run_date`、`source_date`。
- 冻结输入：`account_ids_json`、`material_ids_json`、`body_template`、操作人。
- 状态：`queued|running|completed|completed_with_errors|needs_review|stopped|failed_preflight`。
- 计数：`expected_count`、`queued_count`、`published_count`、`failed_count`、`unknown_count`。
- 审计：错误码/脱敏错误、开始/完成/创建/更新时间。

`x_post_queue` 增量 `manual_run_id INTEGER`。一个正式队列只能关联 daily、catchup、schedule、manual 四类批次中的一个；历史 canary 允许四者均为空。

### API / 接口

- `PUT /api/admin/x-posts/drama-pool/{id}/priority`
  - 请求：`{"high_priority": true|false}`
  - 返回：更新后的安全短剧池 DTO。
- `POST /api/admin/x-posts/material-pool/manual-publish`
  - 请求：`material_ids`、`account_ids`、`idempotency_key`。
  - 返回：HTTP 202、手动批次安全 DTO。
- `GET /api/admin/x-posts/material-pool/manual-runs/{id}`
  - 返回：批次计数、状态、脱敏错误和安全队列结果。
- loopback `/internal/posts/...` 增加对应管理/worker 路由；公开 Nginx 不暴露内部接口。

### 异常与边界

- 所有写接口只接受 Feishu Cookie、实时导航权限和同源 JSON；API Token 拒绝。
- material/account 数量不等、账号重复、素材重复、幂等冲突均在入队前拒绝。
- 不接受页面提供的账号用户名、会员资格、素材 URL、文案或合规结果；全部由服务端读取。
- `unknown_outcome=1` 或 `post_creating` 使批次进入 `needs_review`，剩余队列不得自动恢复。
- worker 崩溃后：无冻结队列的 `running` 批次可重新预检；有冻结队列时只读取并恢复既有队列，不重新选择或建队列。
- 回滚只回滚代码/units/静态文件；保留新增表、列、队列、日志和 token 状态。

## 验收标准

1. 高优仅可作用于未分配可用短剧；操作人和时间可读回，取消后恢复普通排序。
2. 三部已绑定短剧继续绑定原账号；高优不会改绑、跳集或改写冻结计划。
3. 手动按钮位于“加入素材池”上方，独立弹窗选择目标账号并二次确认真实发布。
4. N 个素材必须对应 N 个唯一可发布账号；长视频按当前 token 会员资格安全匹配。
5. 手动提交不新增 `x_post_material_pool` 行，不修改两类 schedule config/random plan。
6. 任一预检失败时 queue/log 均不增加；全部通过时 N 个队列在一个事务中创建。
7. 同素材在素材池、队列、手动并发提交中只能有一个胜者。
8. 请求重放返回同一 manual run；刷新和 worker 重启不导致第二批或第二次 X 写。
9. known failure、限流、unknown 和部分成功聚合符合既有状态优先级。
10. 单元、接口、DOM、迁移和账本审计测试全部通过；部署验收前后 X Post 数量不增加。

## 风险与待确认

- 已确认采用“素材数必须等于账号数、后台自动匹配”的语义。
- 已确认手动队列建立后永久去重，不提供普通失败自动重试。
- 已确认部署只做隔离/只读验收，不创建真实 X Post。
- 外部 X 写无法回滚，已通过二次确认、全批预检、串行发布和未知结果停批降低风险。

## 变更记录

- 2026-08-11：根据用户确认的实施计划建立需求，生产基线对齐 `29bd900`/`3998ee4`。
