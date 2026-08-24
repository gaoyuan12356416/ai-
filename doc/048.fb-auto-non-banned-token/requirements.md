# 048.fb-auto-non-banned-token 需求与技术设计

## 背景

生产 Page 组 62 当前有 13 个有效成员。旧口径仅把
`ads_facebook_page_post.status=0` 且 Token 非空的记录视为可用，因此 8 个
Page 可发、5 个 Page 被记录为 `fb_page_missing_eligible_token`。只读数据库
与 Graph 身份检查证明其中 4 个 Page 的多条 `status=-1` Token 仍能读取
正确 Page，只有 1 个 Page 的所有 Token 都是 `status=1（被封）` 且已被
Meta 拒绝。

## 目标

- 仅排除 `status=1（被封）` 的 Page Token。
- `status=0、-1、2` 以及未来其他非 1 状态，在 Token 非空时均进入候选。
- Page 池统计、运行冻结和执行时凭证读取使用同一口径。

## 范围

### 包含

- Page 组可发布数量查询。
- Page 快照的可用 Token 数量与名称查询。
- Graph 执行前的凭证读取。
- 单元测试、生产只读查询、部署与回滚证据。

### 不包含

- 不修改 `ads_facebook_page_post` 的任何状态或 Token。
- 不重写已经冻结的 run/task/page snapshot。
- 不创建模板、不调用 run-now、不以真实 Facebook Post 验证。
- 不改变 Token 轮换、失败分类、未知结果和对账合同。

## 用户故事 / 业务规则

1. Page Token 只有在 `status=1` 时被业务明确禁止。
2. 非 1 状态仍须满足 `TRIM(page_access_token)<>''`。
3. `eligible_credentials()` 继续要求非空 `fb_user_id`，并按 Token 去重。
4. 每次 execute/reconcile 都重新执行 `eligible_credentials()` 读取动态状态，
   不缓存计划阶段的 Token 列表。
5. 多 Token Page 中无效 Token 仍按现有明确失败轮换合同尝试下一条；未知
   结果不得轮换。
6. 既有冻结任务保持历史事实；执行时动态 Token 立即采用新规则，新的 Page
   统计则从部署后的下一次运行冻结生效。

## 交互与流程

无 UI 操作变化。模板列表的可发布/缺授权数量、运行快照以及执行时授权选择
会自动采用新口径。

## 技术设计

### 影响模块

- `features/fb_auto_posts/repositories.py`
- `scripts/test_fb_auto_repositories.py`
- `doc/003.fb-page-auto-post/requirements.md`
- `doc/003.fb-page-auto-post/api-doc.md`

### 数据结构

无 DDL/DML。只调整只读 SQL 谓词。

### API / 接口

API 路径与字段不变。`publishable_pages`、`missing_token_pages`、
`eligible_token_count` 的业务口径改为“非被封且 Token 非空”。

### 异常与边界

- `status=1` 即使 Token 非空也必须排除。
- 非 1 状态但 Token 为空仍排除。
- 数据库 `status` 为 NOT NULL；无需定义 NULL 兼容行为。
- 本地状态非 1 不保证上游 Token 一定有效；执行器保留现有安全轮换与失败处理。
- `status=2（关闭）` 也纳入候选，这是用户明确要求的“只要不是 1”。

## 验收标准

1. 四处 Page Token SQL 均使用 `status<>1`，不存在残留 `status=0` 资格口径。
2. 定向测试证明 Page 组统计、Page 快照、Page 名称和执行凭证查询一致。
3. FB 专项回归、合并基线和语法检查全部通过。
4. 生产只读查询证明 Page 组 62 为总 13、按新口径可发 12、仅被封 1。
5. 部署后 sidecar 健康、七个 timer 恢复、无提前 Graph 调用或账本异常。
6. 回滚只切回旧代码 release，保留当前 SQLite、Token、wrapper 和发布账本。
7. 回归测试证明每次任务执行都会重新查询 Page Token；明确失败会轮换未使用
   Token，unknown 结果只执行一次并停止自动重发。

## 风险与待确认

- 决策已确认：`status=2（关闭）` 仍执行，因为唯一排除值是 1。
- 可发 Page 增加会提高预制与发布任务量；Page 组 62 从每天 40 个任务变为
  60 个任务，仍低于现有 20 job/slot、500 job/day 门禁，但串行 GPU 预制
  时延需由自然流水线观察。
- 既有 2026-08-25 冻结运行仍为 8 可发/5 跳过，不做历史回填。

## 变更记录

- 2026-08-24：创建需求，冻结“仅 `status=1` 排除”的用户决策与生产边界。
