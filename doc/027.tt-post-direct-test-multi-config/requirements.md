# 027.tt-post-direct-test-multi-config 需求与技术设计

## 背景

现有 TT 素材池页面把“立即发布一条”绑定到自动发布池：请求只选择账号，服务端再领取该账号最早的 `available` 素材。因此运营即使刚校验了一个明确素材 ID，也不能用它立即测试；已消费或已发布过的素材也无法再次测试。该路径还会修改自动池的 FIFO 与消费状态，不适合作为人工验证工具。

页面的描述模板、自动发布开关/时间和 TikTok 账号目前不是一个配置快照，账号只能单选，运营无法直接看出哪些账号属于当前自动发布配置。旧排期还限制不同账号不能使用同一分钟，runner 每轮只领取少量账号；直接取消时间冲突后会产生“同分钟账号未在宽限期内全部落库”的漏发风险。

素材池的 `available/reserved/consumed/canceled` 是自动流程的运营状态，不是平台发布结果。尤其 `consumed` 可能对应失败、阻断或结果未知，不能被展示为“已发布”。

## 目标

1. 增加独立的异步“立即测试”轨道，允许用任意一个已校验素材 ID 对一个明确账号发起测试，包括此前已发布过的素材。
2. 立即测试不读取、不领取、不消费、不重排自动素材池；自动流程继续保持一次性 FIFO 和不复用规则。
3. 对素材统一展示可审计的发布状态，严格使用 `published|unknown|unpublished` 三态；活动任务仍归 `unpublished`，另由尝试次数体现。
4. 将描述模板、自动发布总开关/每日时间、账号多选集合合并为一个原子配置与一个版本。
5. 在账号列表同时提供布尔勾选状态和可读状态值，明确哪些账号已加入自动发布。
6. 支持多个账号使用同一分钟，并保证该分钟全部 due slot 先完成现有 recurring-run 原子预占尝试，再开始任何实时账号网络预检。
7. 以 additive migration 兼容旧逐账号排期；正常回滚不覆盖 SQLite 或 GPU ledger。

## 范围

### 包含

- TT 素材池页面参考 X 素材池的多选、状态标签和独立操作区。
- 选择一个校验成功的素材和一个目标账号，创建独立 direct-test 任务。
- direct-test 的准备、发布、内部核对状态机、幂等与同素材活动/未知阻断。
- 每个 direct-test 创建新的 GPU prepare job，复用现有 GPU prepare/publish 合同。
- 素材发布状态从 `tt_post_queue` 与 `tt_post_direct_test` 的服务端事实聚合。
- 单例自动发布配置、单一乐观锁版本与最多 50 个账号的原子保存。
- 每条入池素材继续显式冻结一个自动归属账号。
- 同分钟所有 due slot 复用 `claim_recurring_run` 的逐项原子预占、去重、崩溃恢复与后续逐条执行。
- 旧 `tt_post_daily_schedule` 的只读投影、首次显式迁移和向旧代码兼容写入。
- CPU sidecar、AI 后台同源代理、静态页面、runner、SQLite additive schema、测试与部署文档。

### 不包含

- 不改变 `tt_post_account_setting` 的隐私、评论、Duet、Stitch、披露和 AIGC 设置模型。
- 不让一个素材自动分发给多个账号，不建立全局随机素材池。
- 不允许“测试目标多账号”；一个测试任务只对应一个账号。
- 不改变 GPU publish ledger 的 key、文件格式、去重或覆写规则。
- 不自动重试 `unknown`，不把未知结果猜成成功或失败。
- 不通过回滚删除或恢复覆盖 SQLite、ledger、manifest、短链 wrapper 或 COS 对象。
- 本需求验收不创建真实 TikTok Post，不保存生产配置，不启停生产自动发布。

## 用户故事 / 业务规则

### A. 独立立即测试

1. 运营批量输入素材 ID并完成服务端校验后，可在任意一条校验成功结果上选择“立即测试”。素材无需已加入自动素材池。
2. 运营必须在独立的单选控件中明确选择测试目标账号；不得复用多账号集合的第一个元素或自动池素材归属账号作为隐式目标。
3. 目标账号可以不属于自动发布账号集合，自动发布总开关也可以关闭，但账号必须存在、具备账号发布设置、通过实时 `creator_info` 与正式 Direct Post 门禁，并获得本次操作确认。
4. 素材过去是否由自动队列或 direct-test 确认发布，不影响再次创建测试；只有上一任务明确终态后用户显式选择“再发一次测试”，才生成新的幂等键和 GPU prepare job。
5. 同一 `idempotency_key` 与同一账号、素材、配置版本、consent version/accepted_at 重复提交，只返回同一个任务；其中任何事实不同均返回 409，不能覆盖原任务。
6. 客户端超时、响应丢失或返回 `queued/preparing/ready/publishing/reconciling` 时必须保留原幂等键，并存储该键对应的 config version 与 consent accepted_at 供精确重放；`unknown` 继续保留等待核对。只有明确 `published|failed|canceled` 后的显式新测试才清除/换键。
7. 同一素材存在 `queued/preparing/ready/publishing/reconciling/unknown` 的 direct-test，或同素材存在活动/未知 legacy queue 时，不得创建新的 direct-test；不同素材可进入任务队列，发布 claim 仍按账号串行。
8. direct-test 只写 `tt_post_direct_test`、短链和 GPU 制品引用；不得写 `tt_post_recurring_pool.run_id/queue_id/status`，不得创建自动 `tt_post_schedule_run` 或 legacy `tt_post_queue`；本需求不新增 event 表。
9. direct-test 的 caption、素材元数据、账号设置、creator-info、短链、用户确认和配置版本在任务创建时冻结；`expected_config_version` 只用于读取并冻结已保存描述模板，目标账号不要求属于该版本的自动发布成员。
10. 服务端每次从素材事实源重新解析源视频和 Drama ID，不信任浏览器缓存的 URL、描述或发布状态。

### B. 自动素材池不复用

1. 自动入池仍要求每个素材请求明确指定一个 `source_account_id`；页面可批量选择素材，但按素材逐条调用入池接口。账号必须属于已保存自动配置，禁止静默使用多选列表中的第一个账号。
2. 一个 `material_id` 在自动 intake/pool 中仍保持唯一；自动流程只按该归属账号的 FIFO 领取一次。
3. direct-test 不消费自动池；自动领取只临时排除同素材 active/unknown direct-test，防止并发或不确定结果下重复初始化。
4. direct-test 到达 `published|failed|canceled` 明确终态后不再阻断自动领取。即使 direct-test 已发布，原本 `available` 的自动池素材仍可按原归属/FIFO 在未来自动发布；direct-test 的任何终态都不改 pool 行。
5. 已被自动流程消费的素材不因 direct-test 失败或配置变化而重新变为 `available`。

### C. 素材发布状态

1. `publication_state=published` 只在 `tt_post_queue` 或 `tt_post_direct_test` 至少存在一条 `status=published` 事实时成立；历史行的 `publish_id` 可以为空，接口仍按持久化状态聚合。
2. `tt_post_recurring_pool.status=consumed`、存在 queue、已准备成片、调用过初始化接口、失败、取消、阻断或错过排期都不等于已发布。
3. 主状态 `publication_state`（并以相同值返回 `publication_status`）只有：
   - `published`：至少有一条确认发布事实；
   - `unknown`：没有确认发布，但至少一条结果未知；
   - `unpublished`：没有发布或未知事实，包含仅有活动/失败/取消任务的情况。
4. 同时返回 `publish_count`、`unknown_count`、`attempt_count`、`latest_published_at_utc`、`latest_publish_id`、`latest_publish_url`、`latest_status_at_utc`。若既有发布又有后续未知，主状态保持 `published`，并保留非零 `unknown_count`。
5. 页面把发布状态和自动池状态分开显示，不使用 `processing` 或嵌套 `publication` 对象。

### D. 一个原子自动发布配置

1. 一个配置快照包含：`caption_template`、`enabled`、`publish_times`、`account_ids`、`timezone=Asia/Shanghai`、确认信息和一个整数 `version`；POST 层使用 `source_account_ids`，响应 item 使用 `account_ids`。
2. v1 每天只支持一个共同的 `HH:MM` 分钟；账号集合最多 50 个、不得重复。
3. UI 的描述、开关/时间、多选账号属于同一个 dirty draft；点击一次“保存自动发布配置”提交全部字段。
4. 服务端先完成全部结构、账号、设置、creator-info、门禁和版本校验，再在一个 `BEGIN IMMEDIATE` 事务中写配置与兼容排期；任一失败整批 0 写入。
5. `expected_version` 不一致返回 409；页面丢弃旧异步响应、重新加载最新快照并要求用户重新确认，不能自动合并。
6. 开启或重新开启时，所有选中账号必须当前有效、已有发布设置、实时能力允许已保存策略、三重门禁满足并有有效确认。
7. 关闭时允许在账号源不可用、账号已失效或门禁关闭的情况下执行“纯关闭/移除”：保留当前模板和时间，只能保留或移除既有成员，不得新增未知账号；不要求新的 creator-info、门禁或用户确认。
8. 关闭态若修改模板或新增账号，模板仍需通过宏/UTF-16 校验，新增账号必须来自当前可信账号快照；真正启用时再次完成实时校验。
9. 保存成功后版本整体加一；不存在成员各自独立版本或部分成功。
10. 模板变更只影响保存后新建的 intake、pool、queue 和 direct-test；历史冻结行不回写。

### E. 多账号成员可见性

1. 账号列表每项必须同时返回并展示：
   - `auto_publish_selected: true|false`；
   - `auto_publish_state: active|paused|attention_required|not_selected`；
   - `auto_publish_config_version`。
2. `attention_required` 必须保留匿名本地占位、勾选态和移除/关闭能力，但不得展示 Token、已过期账号资料或允许重新启用。
3. 搜索、刷新和后台轮询不得覆盖未保存的多选、模板、开关或时间；保存期间锁定相关控件。
4. 多选表示自动发布成员，不代表同一个素材会复制给全部成员。

### F. 同分钟排期

1. 多个选中账号允许相同 `publish_time`，移除旧的跨账号分钟冲突业务限制。
2. scheduler 从启用的兼容 schedule 计算 due slots。门禁开放时，按稳定 run key 对每个 slot 调用现有 `claim_recurring_run`；每次调用各自在一个原子 SQLite 事务中创建 `tt_post_schedule_run(status=claimed)` 并预留该账号精确 FIFO 素材。
3. 所有 due slot 的预占尝试必须结束后，才允许第一次实时 `creator_info` 调用。请求字段 `limit` 只限制后续执行/响应 items，不限制前置预占循环。
4. 相同 tick 重入通过现有唯一 run key 返回既有 run，不生成重复 run 或重复领取素材；不新增 `tt_post_auto_due` 表。
5. 预占成功后进程崩溃，由既有 claimed/unbound run 和 pool reservation 在后续 runner 恢复；每轮必须先预占当前 due slots，再执行旧 recovery，保证 recovery 的 creator-info 也不抢在本轮 preclaim 之前。宽限期不删除已持久化 run。
6. 无素材的 slot 会在 `claim_recurring_run` 时报错并返回 skipped 信息，不创建空 run 行；该失败不得阻止其余 slots 的预占或执行。各预占事务独立，不承诺整批全成或整批回滚。

## 交互与流程

### 页面结构

1. “选择 TikTok 账号”改为可搜索多选列表；每行显示复选框、账号名、ID、发布能力和自动发布成员状态。
2. 描述模板、自动开关/时间、多选账号放在同一配置卡，只有一个保存按钮、一个 dirty 状态和一个版本提示。
3. “素材校验结果”每行显示发布状态、自动池状态、Drama ID，并提供“立即测试”操作。
4. 自动入池区另有“本批素材归属账号”单选；测试弹层另有“测试目标账号”单选，两者不能从多选集合隐式推导。
5. direct-test 创建成功后展示任务 ID、素材、目标账号、prepare job、当前阶段、更新时间和安全错误；轮询任务状态，不把“已提交”显示成“已发布”。

### direct-test 流程

1. 浏览器校验素材并选择单一测试账号。
2. 浏览器加载最新已保存自动配置版本和账号发布设置，展示最终 caption 预览与确认框。
3. `POST /api/admin/tt-posts/test-publish` 提交最小事实和幂等键。
4. 服务端重新解析素材、实时校验账号、冻结配置/策略，创建 `queued` direct-test 与新的 `gpu_job_id`。
5. prepare runner 领取任务并调用既有 GPU `/internal/tt-post/prepare`；成功后写 `ready`。
6. publish runner 领取 ready 任务，执行正式门禁和账号串行检查，再调用既有 GPU publish；明确结果写 `published/failed`，不确定写 `unknown` 并停止自动重试。
7. UI 通过 `GET /api/admin/tt-posts/direct-tests` 列表（可按账号、素材、状态筛选）轮询任务；只有 `published` 才更新素材“已发布”事实。

### 原子配置流程

1. `GET /auto-config` 返回 config version 与账号集合；`GET /accounts` 在每个账号上返回成员布尔、四态值和 config version。
2. 用户在一个草稿中修改模板、开关/时间和账号多选。
3. 页面本地校验后提交完整快照与 `expected_version`。
4. 服务端完成全量校验并在一个事务中写单例配置、选中账号的兼容 schedule、移除账号的禁用状态。
5. 成功后返回新版本；失败则所有字段和账号都不落库。

### 旧排期迁移

1. additive migration 只建新表/索引，不改旧 `tt_post_daily_schedule` 行，不自动写单例配置。
2. 单例配置不存在时，GET 返回 `version=0` 的只读兼容 item：账号来自当前启用的旧排期，字段包括 `legacy_review_required`、`legacy_schedule_mode` 与 `legacy_publish_times_by_account`，且不写数据库；保存后的 item 另返回 `legacy_membership_mode=atomic`。
3. 旧启用排期只有一个共同分钟时，页面可预填该分钟，但仍需运营首次显式保存。
4. 旧启用排期的完整时间元组不一致时返回 `legacy_review_required=true`、`legacy_schedule_mode=mixed` 和 `publish_times=[]`；禁止将时间并集交叉应用给每个账号。
5. mixed 首次显式保存必须提交统一时间并保持 `enabled=false`；该事务成功后，下一次保存才可启用。单例配置写入、选中账号 schedule 同步、移除账号 schedule 禁用在同一事务完成。
6. 保存前旧 runner 继续按旧排期运行；保存后旧/新 runner 都读取已经同步的逐账号 schedule，避免切换窗口双跑。

## 技术设计

### 影响模块

- `features/tt_posts/core.py`：两张 additive 新表、原子配置、direct-test 状态机、发布状态聚合，以及既有 recurring-run 原子预占。
- `features/tt_posts/service.py`：账号/素材重解析、实时能力校验、管理 API、direct-test 编排和状态查询。
- `scripts/tt_post_prepare_runner.py`：异步领取 direct-test prepare；每次测试使用新的 job ID。
- `scripts/tt_post_runner.py`：先尝试预占同分钟全部 due slots，再执行自动 queue 与 direct-test publish。
- `app.py`：同源代理、权限、审计和错误透传。
- `static/tt-post-pool.html`：原子配置卡、账号多选/状态、素材发布状态、单目标立即测试。
- `features/tt_posts/links.py`：direct-test 短链身份与既有 X/TT namespace 回归（如实现需要）。
- `scripts/test_tt_*.py`：core/service/runner/UI/app/GPU/短链回归。

### 数据结构

#### `tt_post_auto_publish_config`

- 单例 `id=1`。
- 保存 `version/enabled/timezone/publish_times_json/account_ids_json/caption_template/consent/audit timestamps`。
- v1 服务层强制 `publish_times_json` 为空或仅一个元素；JSON 仅用于兼容既有 schedule 数据形状。
- 全字段在一个 SQLite 事务中更新，`version` 是唯一乐观锁。

#### `tt_post_direct_test`

- `idempotency_key UNIQUE` 与 `request_sha256` 保证同键同任务。
- 不对 `material_id` 建唯一约束；同一素材允许多条历史任务。
- `gpu_job_id UNIQUE`，每个新任务生成新值。
- 冻结素材、caption、短链、账号、creator-info、账号设置、确认、config version 和 prepare 输出事实。
- 状态：`queued -> preparing -> ready -> publishing -> reconciling -> published|failed|unknown|canceled`。
- `unknown` 不由发布重试自动离开；由内部 reconciliation 按 GPU ledger/远端证据核对。
- 不存在 `tt_post_direct_test_event`；审计字段和任务状态保存在任务行及现有日志中。

#### recurring run 与历史表

- 复用现有 `tt_post_schedule_run`、`tt_post_recurring_pool` 和 `claim_recurring_run`；不新增 due 表，也不修改现有 run schema。
- 每个 due slot 是一次独立原子预占；全量预占循环结束前不得进行 `creator_info` 网络调用。
- `tt_post_recurring_pool` 与 `tt_post_queue` 的 material 唯一/一次性规则保持。
- 禁止为重复 direct-test 放宽 legacy queue/pool 的 `material_id UNIQUE`。

#### 发布状态投影

- 使用查询/视图按 `material_id` 聚合 queue 与 direct-test，不回写一个容易漂移的“已发布”布尔字段。
- 聚合必须忽略 pool `consumed` 推断，返回三态与计数/最新事实字段。

详细 schema 草案见 `migration.sql`；该文件只供评审，真正 migration 由实现代码幂等执行并测试两次运行。

### API / 接口

- `GET /api/admin/tt-posts/auto-config`
- `POST /api/admin/tt-posts/auto-config`
- `POST /api/admin/tt-posts/test-publish`：创建独立立即测试。
- `GET /api/admin/tt-posts/direct-tests`：分页/筛选查询测试任务。
- `POST /api/admin/tt-posts/materials/preview`：响应合并发布状态字段。
- `GET /api/admin/tt-posts/material-pool`：响应合并发布状态字段，summary 增加三态计数。
- `POST /api/admin/tt-posts/material-pool`：一次一个素材，要求单一 `source_account_id` 与 `expected_config_version`。
- `POST /internal/tt-posts/schedules/due`：请求只接受 `limit`；先完成全部 slot 预占尝试，再执行至多 limit 项。
- `GET /internal/tt-posts/direct-tests/reconciling` 与 `POST /internal/tt-posts/direct-tests/{id}/reconcile`：仅供内部 runner 核对。

旧 `POST /api/admin/tt-posts/run-now` 继续作为兼容自动池手工触发接口存在；新页面的立即测试不得调用它。

### 异常与边界

- 空素材、未校验素材、多个/空测试账号、未知账号：400/409，0 direct-test、0 GPU 调用。
- 自动配置版本冲突：409，配置、成员、旧 schedule 全部 0 写入。
- 同幂等键不同请求：409，返回原任务标识，不修改任何冻结字段。
- 同素材存在活动/未知 direct-test 或 legacy queue：409，禁止新 direct-test，等待明确终态或内部核对。
- GPU prepare 失败：direct-test `failed`；不创建 legacy queue、不修改 pool。
- publish 响应不确定：direct-test `unknown`；不得自动重试或新建替代任务。
- 自动配置关闭时不要求有效 consent/creator-info；已有 consent 由 core 保留。启用时才要求 `consent.accepted=true` 与全部成员实时校验。
- 模板含未知宏、缺少 Drama ID、渲染后超过 2200 UTF-16 units：400，0 写入。
- 保存后旧异步 GET 返回：前端按 request generation/version 丢弃，不覆盖新状态。
- 同分钟账号超过执行 `limit`：全部 slot 仍先尝试原子预占；成功但未执行的 run 进入既有 recovery backlog。
- 直接测试已发布素材：允许创建新任务，但仍受同素材活动/未知阻断。

## 验收标准

| 编号 | 验收项 | 通过条件 | 优先级 |
| --- | --- | --- | --- |
| AC-01 | 重复素材立即测试 | 已确认发布素材用新幂等键创建新 direct-test；新 GPU job；无 pool/queue/run 变化 | P0 |
| AC-02 | 测试明确目标 | 请求必须恰好一个素材和一个账号；不从多选或 pool 推断 | P0 |
| AC-03 | direct-test 幂等 | 同键同请求返回同 ID；同键异请求 409 且 0 写入 | P0 |
| AC-04 | unknown 阻断 | 同素材 unknown 后原键重放返回原任务；新键测试被阻断，其他素材不被此规则误阻断 | P0 |
| AC-05 | 自动池隔离 | direct-test 任意终态均不修改自动池状态、顺序、run_id、queue_id | P0 |
| AC-06 | 自动互斥边界 | 自动流程不重领自己已消费的素材；同素材 direct-test active/unknown 时暂不领取，direct-test 明确终态后仍按原 pool/FIFO 可领取 | P0 |
| AC-07 | 发布状态正确 | 只用 queue/direct-test 的 `status=published`；失败消费显示未发布；unknown_count 独立保留 | P0 |
| AC-08 | 原子配置 | 模板、总开关/时间、账号集合一次提交、一个版本、全部成功或 0 写入 | P0 |
| AC-09 | 版本冲突 | 任一并发冲突返回最新版本，不出现模板/账号/schedule 部分更新 | P0 |
| AC-10 | 无效账号 | 开启时一个账号无效/无设置/能力失败，整批 0 写入；纯关闭/移除仍可完成 | P0 |
| AC-11 | 成员可见 | 每账号同时有 `auto_publish_selected`、四态 `auto_publish_state` 和 config version；刷新不覆盖 dirty draft | P1 |
| AC-12 | 单素材单归属 | 入池必须显式单账号；多选不会复制素材或使用首项 | P0 |
| AC-13 | 同分钟预占优先 | 50 个账号同分钟且均有素材，首次 tick 在第一个 creator-info 前完成 50 次 claim 尝试；成功项都有稳定 run/FIFO reservation，重入无重复 | P0 |
| AC-14 | 旧排期迁移 | 多旧时间进入 review required；无静默并集/交叉；首次保存事务失败保留旧数据 | P0 |
| AC-15 | 冻结不漂移 | 配置修改不改变既有 pool/queue/direct-test 的模板、账号和事实快照 | P1 |
| AC-16 | 无真实副作用 | 自动化用临时 DB/fake GPU；生产只读验收前后配置、pool/queue/run/direct-test/ledger/Post 基线相同 | P0 |
| AC-17 | 回滚保留历史 | 回退代码/静态资源后 SQLite 新表、direct-test、unknown、ledger、manifest、COS 均保留 | P0 |

## 风险与待确认

### 已锁定，不再作为待确认项

- direct-test 每次重新 prepare，不复用自动池成片，因此无需修改 GPU ledger 规则。
- direct-test 目标为独立单账号，可不属于自动成员；配置版本只冻结保存模板；自动素材仍单账号归属且必须属于自动成员。
- auto/direct 互斥只覆盖同素材 active/unknown；direct-test `published|failed|canceled` 不永久阻断自动 pool。
- v1 多账号共用一个每日分钟；旧多时间不自动合并。
- 验收禁止真实 Post 和生产配置保存。

### 实现风险

- **P0：旧时间交叉放大。** 旧账号不同分钟若取并集并写给每个账号，会增加每日发布次数；必须以 migration review 阻断。
- **P0：unknown 重复发布。** HTTP 超时后生成新 key 可能重复发同一素材；必须服务端素材级阻断，不能只靠 sessionStorage。
- **P0：表唯一约束误用。** 复用 legacy queue 会被 `material_id UNIQUE` 阻断；放宽该约束又会破坏自动池不复用。必须使用独立表。
- **P0：同分钟漏发。** `limit=1` 不能截断预占循环；所有 due slot 的 `claim_recurring_run` 尝试必须先于首个 creator-info。
- **P0：配置部分保存。** 先逐账号写 schedule 再写模板会造成混合版本；必须一个 SQLite 事务与一个 expected version。
- **P1：状态误导。** “已消费”不能复用为“已发布”；UI/API 必须双状态展示。
- **P1：成员与素材归属混淆。** 多选后需要独立的批次归属单选和测试目标单选。
- **P1：50 账号实时检测耗时。** 开启前能力检测并发需有上限；失败整批终止，不能部分启用。

## 变更记录

- 2026-08-03：初版。
- 2026-08-03：按实现复核修订：立即测试改为 `/test-publish` 且账号独立于自动成员；发布状态改为三态扁平字段；同分钟复用 `claim_recurring_run` 逐项原子预占，不新增 due/event 表；旧 `/run-now` 保持兼容。
