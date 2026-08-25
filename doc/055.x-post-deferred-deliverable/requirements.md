# 055.x-post-deferred-deliverable 需求与技术设计

## 背景

生产素材池中 5 条素材在 2026-08-21 入池时因关联短剧的可投放时间为
2026-08-22 00:00（北京时间）而记录 `drama_not_yet_deliverable`。截至
2026-08-25，这些记录仍停留在首次校验时间、页面显示“不可用”，且没有再次进入
自动排期候选。现有需求文档约定“到时自动恢复”，实现却没有把该错误纳入可复检
候选集合，形成永久冻结。

## 目标

1. 未到短剧可投放时间的素材显示为“待可投放”，而不是“不可用”。
2. 可投放时间之前不创建队列、不调用 X；到时后由下一次自然素材排期重新校验，
   校验通过后才允许冻结队列和发布。
3. 历史 `drama_not_yet_deliverable` 记录无需删掉重加即可自动恢复。
4. 汇总一份覆盖 X 素材发布链路的错误目录，供运营检查错误码、中文说明、是否自动
   重试及处理建议。

## 范围

### 包含

- 素材池候选查询、FIFO 校验、入池统计和派生可用状态。
- 主 API 查询参数、素材池 UI 状态/提示/筛选。
- 历史未绑定、未发布的 `drama_not_yet_deliverable` 记录兼容。
- 单元、契约、静态页面和无真实发帖的生产验收。

### 不包含

- 不提前为未来素材创建 X 队列，也不创建独立的逐素材定时任务。
- 不改变短剧源表 `deploy_time`，不修改 X 账号、Token、发布模板或发布时间配置。
- 不重放历史失败/未知队列，不以真实 X Post 作为验收手段。
- 不把真正的素材/合规/媒体失败放宽为可发布。

## 用户故事 / 业务规则

1. 入池时若最新 Dramawave `deploy_time` 严格晚于当前时间，池主状态仍为
   `unpublished`，保存错误审计，但派生状态为 `deferred`（待可投放）。
2. `deferred` 不计入“可供发布”，也不计入“校验失败/不可用”。
3. 自动素材排期必须扫描 `deferred` 记录并重新读取真实 `deploy_time`；边界前仍跳过，
   边界相等或已过去时才可进入完整媒体、账号和合规预检。
4. 边界前的临时跳过允许当前批次继续扫描后续素材，不得阻断 FIFO 的可用子集。
5. 只有当前完整预检成功且素材被选中时，才能在创建队列的同一 SQLite 事务中清除
   旧错误；不得通过列表查询、页面刷新或独立清错提前变为可发布。
6. 任何已有关联队列、已发布或未知结果的素材都保持原有不可重用合同。
7. 生产验收只允许只读 API/数据库、自然 timer、离线 fixture 和账本计数对比。

## 交互与流程

`入池校验 -> deploy_time 未来 -> 待可投放（无队列） -> 后续自然排期复检 ->`
`仍未来则继续等待 / 到时且完整预检通过则原子清错并建队列 -> 正常顺序发布`。

页面状态文案：

- `deferred`：待可投放；显示可投放时间说明和自动复检提示。
- `validation_failed`：不可用；仅用于真正未通过 X 发布标准的素材。

## 技术设计

### 影响模块

- `features/x_posts/service.py`
- `app.py`
- `static/x-post-material-pool.html`
- `scripts/test_x_post_material_pool.py`
- `scripts/test_x_post_multi_schedule_store.py`
- `scripts/test_x_accounts_app_contract.py`
- `scripts/test_x_post_error_catalog.py`（新增）

### 数据结构

无数据库迁移。继续复用 `last_error_code`、`last_error_message`、
`last_checked_at`；`deferred` 是 API 派生状态，不是池主状态，也不是队列状态。

### API / 接口

- `GET /api/admin/x-posts/material-pool` 的 `availability` 新增 `deferred`。
- `summary` 新增 `deferred` 计数，`available` 继续只表示当前可供发布。
- `POST /api/admin/x-posts/material-pool` 响应新增 `deferred_count`；
  `validation_failed_count` 不再包含未来可投放素材。
- 内部候选查询响应格式不变，但会返回需复检的未来时间记录。

### 异常与边界

- `deploy_time == now`：允许；`deploy_time > now`：等待。
- 多端记录仍取最晚 `deploy_time`；缺失、非法、超范围仍 fail closed。
- 临时时间错误只有本次预检真实刷新 `last_checked_at` 后才可作为 FIFO 跳过证据。
- 临时时间错误被选中时，候选必须携带本轮 selector 返回且已到点的
  `drama_deploy_time`；该证明只用于原子 FIFO 门禁，不写入 queue schema。
- 配置中没有部署时间之后的发布槽时，素材等待下一次自然槽；系统不私自新增发帖时点。
- 即使到时，账号/语言/合规/媒体/Token/X 上游任一后续门禁仍可阻止发布。

## 验收标准

1. 构造未来时间记录时，API 返回 `availability=deferred`，汇总和筛选一致。
2. 入池统计返回 `deferred_count=1`、`validation_failed_count=0`、
   `available_count=0`。
3. 自动候选查询会重新返回历史 `drama_not_yet_deliverable` 记录。
4. 同一素材在边界前不建队列；边界后完整预检成功可原子清错并建队列。
5. 未来素材被跳过时，后续当前可用素材仍可形成最大非空子集。
6. 页面显示“待可投放”和中文复检说明，不显示为红色“不可用”。
7. 线上 5 条历史素材在部署后由自然排期重新校验；不人工创建真实 X Post。
8. 错误目录按阶段列出代码、中文解释、自动动作、是否可重试和运营处理建议。

## 风险与待确认

- 历史 5 条素材到时后仍可能因其他门禁失败；这属于新的真实预检结果，不应强行发布。
- 固定 `scan_limit` 内大量未来素材会增加源表复检量，但不会提前占用队列。
- 生产自然排期可能在部署窗口内恰好到点；上线前后必须冻结账本计数并观察 timer，
  不能把自然业务发布误归因于部署测试。

## 变更记录

- 2026-08-25：根据生产截图和只读账本证据创建需求；确定使用派生
  `deferred` 状态和自然排期复检，不提前建队列。
