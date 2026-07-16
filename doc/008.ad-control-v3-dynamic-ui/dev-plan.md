# 开发计划与完成记录

## 1. 开发边界

分支 `codex/ad-control-v3-dynamic-ui` 从 V2 冻结基线 `2b52bc8d06b8a36a473dad8916012570ee28c15b` 建立独立 worktree。实现只新增 V3 feature、动态页面、API、八表 SQL、数据盘存储、禁用 runner、测试和 exact-source 部署器；旧 V2 不改造。

本期完成：FB 两个动态页面、产品多选/optimizer/可选时区、三层规则、配置 CRUD、范围估算、手动 observe、执行日志、ads_ai repository、数据盘快照、权限和发布器。

本期未完成且不可发布：scheduler、enable、live pause、live copy、TikTok、copied created_data、快照清理器。

## 2. 最终文件结构

```text
features/ad_control_v3/
├─ routes.py                 # 动态路由、认证、同源与方法边界
├─ page_renderer.py          # 两个动态模板和 allowlist asset
├─ schemas.py                # 严格 payload、三层 Copy carrier
├─ catalog.py                # 字段能力与 optimizer resolver
├─ channels/{base,facebook,tiktok}.py
├─ rule_engine.py
├─ repository.py             # ads_ai 八表 allowlist、读写分离、事务
├─ storage.py                # 数据盘原子 gzip 快照
├─ service.py
├─ templates/{rule-groups,execution-logs}.html
└─ assets/{app.js,app.css}

scripts/ad_control_v3_runner.py       # 默认 disabled；scheduler 未连接
deploy/apply_ad_control_v3.py         # exact-source check/apply/rollback
deploy/apply_ad_control_v3_navigation.py # 现场 navigation 键级合并/回滚
doc/008.ad-control-v3-dynamic-ui/sql/
tests/test_ad_control_v3_{core,repository,routes,ui,deploy}.py
```

`app.py` 只有 V3 lazy dispatcher 的纯新增 overlay。`static/navigation.json` 增加一个 V3 分组和两个动态链接；生产发布时必须基于现场 JSON 做键级合并，不能整份覆盖。

## 3. 数据模型

| 表 | 本期用途 | 写入时点 |
| --- | --- | --- |
| `ad_control_v3_product_catalog` | 审核后的短剧产品枚举 | 独立 seed 检查点 |
| `ad_control_v3_rule_group` | 配置、版本/hash、owner/optimizer、Preview 指针 | CRUD/Preview CAS |
| `ad_control_v3_rule_group_product` | 多选产品关系 | CRUD |
| `ad_control_v3_preview` | 不可变 Preview 摘要与快照索引 | 手动试算事务 |
| `ad_control_v3_preview_target` | Preview 对象合同 | 手动试算事务 |
| `ad_control_v3_execution` | observe 事件摘要 | 手动试算事务 |
| `ad_control_v3_execution_target` | 对象级动作/阻断/原因 | 手动试算事务 |
| `ad_control_v3_runner_event` | 未来 scheduler 的幂等/lease 预留 | 本期不写 |

所有表固定在 `ads_ai`；运行时无 DDL。Preview/Execution bundle 由 63353 writer 单事务写入，查询由 63350 reader 完成。源业务库只读。

## 4. 任务状态

| 工作项 | 状态 | 证据/备注 |
| --- | --- | --- |
| 独立 worktree、分支和本地 source bundle 备份 | 已完成 | worktree/branch 已建立；备份 hash 已记录在部署交接 |
| 生产源表、身份、Nginx、数据盘只读核实 | 已完成 | `ads_custom_source_insight`、optimizer、`/mnt/data-disk`、现网 app hash |
| 需求、SA、API、SQL、测试与部署文档 | 已完成 | 本目录 |
| 动态路由、认证、CSP、两页和 asset allowlist | 已完成 | routes/UI tests |
| 产品 + optimizer + 时区范围和三层 adapter | 已完成（本地） | bounded-query/core tests；生产 EXPLAIN 待执行 |
| 规则 schema、字段目录、规则引擎和 Copy 参数 | 已完成 | core/UI tests |
| ads_ai repository、八表 DDL/seed、事务与 CAS | 已完成（代码/SQL） | repository tests；生产 DDL 待执行 |
| 数据盘原子快照、校验和、权限和大小门禁 | 已完成（代码） | storage tests；生产 mount 待执行 |
| 手动 Preview、observe execution 和执行日志 | 已完成（本地） | core/repository/UI tests |
| exact-source check/apply/rollback | 已完成（本地） | deploy tests |
| 132 条 V3 自动化 | 已完成 | 132/132；含 product 精确匹配、字段投影/查询熔断与 navigation 发布链 |
| 本地 Playwright 1440/390 视觉验收 | 已完成 | 5 张截图；console 0/0 |
| Git commit/push/服务器精确 fetch | 待主发布流程执行 | 未在本文伪造 commit |
| 生产八表 DDL/15 产品 seed/schema 回读 | 待执行 | 需 63353 写、63350 回读 |
| 生产动态页面、真实登录、手动 observe | 待执行 | 仅允许 Meta 写 0 |
| V2 全量回归与自然 tick 前后比对 | 待执行 | 生产发布门禁 |
| scheduler/timer、规则 enable | 后续版本 | runner 固定失败关闭 |
| live pause/copy、TikTok、created_data 写入 | 后续版本 | 当前无外部 mutator |
| 快照补偿/清理器 | 后续版本 | 当前不自动删除孤儿快照 |

## 5. 已实现流程

### 配置

1. 登录并加载 `/meta`。
2. 选择 Facebook、短剧产品、optimizer、可选时区和对象层级。
3. 通过字段能力目录配置条件、pause/copy、候选选择、计划/额度和 Copy 参数。
4. 服务端递归拒绝账户字段，校验 optimizer、产品、层级字段和全部数值。
5. 新建固定保存为 disabled/observe；更新递增 version、重算 behavior hash 并失效旧 Preview。

### 手动 observe

1. 明确 `metric_window_days` 或 `date_from/date_to`。
2. adapter 对每个产品执行 bounded FB query：强制 `dpdo`、`data_source IN (0,6)`、platform/product/dt/optimizer，源 session 与 hint 均为 8 秒、source socket 9～10 秒、总扫描 soft deadline 15 秒；scope 只投影身份/父级，Preview 只投影规则条件、Top N 和 Copy CPI 依赖；可索引 `s.product=%s` 前置条件加 `BINARY` exact 复核。时区留空完全不联设置表。
3. rule engine 匹配条件，pause 优先，应用 Top N 和稳定排序。
4. 先将不可变 gzip 快照原子写入数据盘。
5. 在同一 MySQL 事务写 Preview、两类 targets、observe execution，并 CAS 更新规则组 Preview 指针。
6. 返回最多 200 个目标预览和完整计数，`meta_write_count=0`。

若第 5 步失败，快照可能成为未引用文件；本期不自动清理，也不会产生可执行状态或 Meta 写。

## 6. 未发布流程的失败关闭

- `scripts/ad_control_v3_runner.py`：`AD_CONTROL_V3_RUNNER_ENABLED` 未设时输出 disabled；即使 runner/release 两开关都开启，也返回 `runner_scheduler_not_configured`。
- 启用 observe 规则：返回 `runner_scheduler_not_configured`。
- live pause：返回 `live_pause_disabled`。
- live copy：返回 `copy_persistence_not_configured`，发生在 Token/Graph 前。
- TikTok：返回 `channel_not_enabled`。
- Budget/Meta status 等 roadmap 字段：保存时返回 `field_not_supported`。

## 7. 本地验证命令

已由主流程执行并记录 132/132：

```powershell
python -m unittest discover -s tests -p "test_ad_control_v3*.py" -v
```

最终 commit 前仍需重跑：

```powershell
python -m py_compile app.py scripts/ad_control_v3_runner.py
python -m compileall -q features/ad_control_v3
node --check features/ad_control_v3/assets/app.js
python -m unittest discover -s tests -p "test_ad_control_v3*.py" -v
git diff --check
```

生产 staging 必须使用 Python 3.9 再执行相同语法和 V3 suite；真实 MySQL/页面/V2 回归见 `deploy.md`。

## 8. 发布阶段

1. 本地冻结：重跑测试、代码评审、只 stage 本需求文件。
2. GitHub：commit/push 精确 `codex/` 分支。
3. 服务器 staging：fetch 精确 source/target commit，运行 deployer `--check` 与测试。
4. 数据盘：创建配置、cache、快照和 backup 根，校验 mount、权限和空间。
5. 数据库：独立执行八表 DDL、schema 回读、15 产品 seed；失败先停，不部署 route。
6. Route dark：导航先隐藏，exact-source overlay，重启唯一必要 API service。
7. 线上只读：认证/权限/旧 V2/页面/asset/API 健康。
8. 手动 observe：单 optimizer、单产品、单层；三层逐个扩大，持续证明外部写为 0。
9. 导航：从现场 JSON 键级合并 V3 两链接。

本期到第 9 步即结束；不得继续创建 timer、enable 规则或做 Meta Canary。

## 9. V2 零影响门禁

- 旧静态页、旧 feature、旧 runner 文件 hash 前后一致。
- `/api/ad-control/*` 旧契约和 owner 隔离回归通过。
- V2 SQLite online backup/integrity/schema/row hash 前后一致。
- ad-control cron 行、锁、日志和自然 tick 前后一致。
- V3 路径以外请求不导入 V3、不访问 V3 DB/数据盘。
- `ads_ai.ad_control_action_log` schema/行数不因 V3 迁移变化。

## 10. 回滚检查点

| 检查点 | 回滚 |
| --- | --- |
| 本地未发布 | 删除 V3 worktree/分支；V2 不受影响 |
| DDL 已建但八表为空 | 经审批执行 `900_rollback_empty_v3_tables.sql`；脚本先验证全表无真实数据 |
| 已有配置/Preview/日志 | 停止 V3 入口和写入，保留表及快照，不 DROP |
| overlay 已完成 | 使用同一 deployer `--rollback`、精确 source/target、deployment lock 和数据盘 checkpoint；目标有漂移则停止 |
| navigation 已发布 | 基于现场备份仅回滚 V3 分组，不覆盖其他导航项 |

## 11. 遗留风险

- 生产大表 `dpdo` EXPLAIN、8 秒 session/hint 行为、规则字段组合读压和复制延迟尚未完成发布前验证。
- 生产 optimizer 身份和 active 产品 seed 尚未验证。
- 快照与 MySQL 跨存储不是原子事务；DB 失败会留下未引用快照。
- scheduler/额度/账户本地时间语义尚无实现，计划字段仅配置。
- Copy 的 Meta 结构、created_data 和绩效关联合同尚未实现。
- 三层 live pause 均没有 Canary 结论。

## 12. 完成记录

- 2026-07-16：完成 V3 本地实现、132/132 自动化和 1440/390 Playwright 视觉证据。
- 2026-07-16：标准流程文档按最终边界收口；生产/MySQL/V2 回归仍待主发布流程执行。
