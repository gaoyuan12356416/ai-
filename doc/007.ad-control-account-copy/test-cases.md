# 测试用例

## 测试范围

规则模型、权限、冲突消解、计划/额度/冷却、Campaign 复制编排契约、生产复制前置熔断、UI payload、runner、既有 Campaign 关闭回归。

## 测试数据

- 临时 SQLite。
- Stub Meta，构造 CBO、ABO、兼容/不兼容 ROAS、N 条 Ad、超时和进程退出；仅供隔离编排模块测试。
- 两个用户、两个账号时区、剧目映射缺失或歧义数据。
- MySQL 使用 fake/spy，证明本期没有 copied created_data/lineage/intent DDL/DML；既有 `ads_ai.ad_control_action_log` 适配器可单独 mock 验证其原有审计写入，但该表不是复制结果落表。测试不得连接生产库。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 新规则组安全默认值 | 当前用户登录 | 只提交名称和账号 | `enabled=false`、`run_mode=observe`、`object_level=campaign` | P0 | 通过（自动化） |
| TC-002 | 禁止代他人设置 | payload 含其他 owner | 保存规则组 | 返回 `owner_forbidden`，无写入 | P0 | 通过（自动化） |
| TC-003 | 正式模式二次确认 | observe 组 | 不带/带正确确认切 live | 前者拒绝，后者允许但仍保持 disabled | P0 | 通过（自动化） |
| TC-004 | 观察模式零业务副作用 | 命中 pause/copy | runner 执行 | Token/Graph/Meta 写 0 次、复制结果表写 0 次，仅允许既有 action log 审计 `would_pause/would_copy` | P0 | 通过（自动化） |
| TC-005 | 关闭优先 | 同对象同时命中 copy/pause | 试算 | target 为 pause，copy 记 `shadowed_by_rule` | P0 | 通过（自动化） |
| TC-006 | 正式复制前置熔断 | live 组命中 Campaign copy | 执行复制 | 返回 `copy_persistence_not_configured`，Token/Graph/Meta POST 0 次、复制 created_data/lineage/intent 写 0 次；可写既有 action log | P0 | 通过（自动化） |
| TC-007 | 深复制调用顺序（隔离模块） | Stub Meta、来源含 N 条 Ad | 运行纯编排模块 | 先 PAUSED 深复制，再轮询和映射；不接入 app/runner | P1 | 通过（隔离自动化） |
| TC-008 | 映射不完整（隔离模块） | 新对象少一条 Ad | 运行纯编排模块 | 返回 `copy_mapping_mismatch`，不得激活 | P1 | 通过（隔离自动化） |
| TC-009 | 崩溃幂等恢复（隔离模块） | Stub 已复制后模拟退出 | 相同 intent 重跑 | 不再次调用 deep copy，只恢复轮询/映射 | P1 | 通过（隔离自动化） |
| TC-010 | 本期无复制落表变更 | 启动、保存、试算、runner | 检查新增 SQL 和迁移文件 | 生产路径无 copied created_data/lineage/intent DDL/DML、无复制结果目标表依赖；既有 action log 不变 | P0 | 通过（静态审查） |
| TC-011 | 每日额度（隔离模块） | 临时 SQLite intent 达到规则/用户/部署上限 | 再次选择候选 | 跳过并记录对应 quota reason；不接入生产 app/runner | P0 | 通过（隔离自动化） |
| TC-012 | 账号时区和截止时间 | 两个不同时区账号 | 在边界前后计算 | 各按账号本地时间执行，未知时区 fail-closed | P1 | 通过（自动化） |
| TC-013 | 冷却期（隔离模块） | 临时 SQLite 中来源 24h 内已有 intent | 再次命中 | 不创建新 intent、不复制；不接入生产 app/runner | P0 | 通过（隔离自动化） |
| TC-014 | 稳定 Top N | ROAS/消耗相同 | 多次试算 | 按对象 ID 稳定排序，结果一致 | P1 | 通过（自动化） |
| TC-015 | CBO 预算（隔离模块） | Stub Meta、CBO 来源 | 运行纯编排模块计算 `X*CPI` | 传给 Stub 的新 Campaign 预算正确，Ad Set 不写 ABO 预算；不接入生产 app/runner | P0 | 通过（隔离自动化） |
| TC-016 | ABO 预算分配（隔离模块） | Stub Meta、多 Ad Set ABO 来源 | 运行纯编排模块计算来源预算比例 | 传给 Stub 的新 Ad Set 预算按来源占比分配且总额一致；不接入生产 app/runner | P0 | 通过（隔离自动化） |
| TC-017 | 不兼容 ROAS | 非兼容出价模式 | 配置 ROAS +/- | 候选跳过，不改变竞价策略 | P0 | 通过（自动化） |
| TC-018 | Ad 第二阶段熔断 | `object_level=ad` | 保存、启用、试算、runner、正式执行 | 配置可保存；启用及其余执行顶层入口返回 `phase_not_enabled`，不出现 Campaign 伪候选，0 Token/Graph/Meta 写 | P0 | 通过（自动化） |
| TC-019 | 剧目映射失败关闭 | 指定剧/最近 X 天且映射缺失或歧义 | 扫描候选 | 跳过并记录明确原因，不按 Campaign 名推断 | P0 | 通过（自动化） |
| TC-020 | 既有关闭规则回归 | 旧规则组 | 试算和执行关闭 | 行为保持，复制熔断不影响 pause | P0 | 通过（自动化） |
| TC-021 | 页面 payload | UI 配置所有新字段 | 保存/回显/立即试算 | 字段完整且无 product/owner_user_id 控件 | P1 | 通过（自动化） |
| TC-022 | 旧聚合组迁移 | 同一 `frontend_rule_group_id` 下有多个旧组，部分启用 | 编辑保存 | payload 仅提交除新 `group_id` 外的 `migrate_from_group_ids`；后端同事务保存新组并禁用、软删除旧组，失败时旧组不变 | P0 | 通过（自动化） |
| TC-023 | 部分启用展示 | 旧聚合组一部分 enabled | 打开规则组列表 | 显示 `partial_enabled`/“部分启用”，不得显示“已禁用” | P0 | 通过（自动化） |
| TC-024 | Legacy observe 与未知 action | 旧规则含 `action=observe` 或未知值 | 打开编辑并保存 | observe 显式提示并保存为 `run_mode=observe` + `action=pause`；未知 action 拒绝保存，不得静默改成 pause | P0 | 通过（自动化） |
| TC-025 | Stale preview 执行熔断 | 试算后修改规则、禁用或急停 | 使用旧 preview 正式执行；并在多对象 pause 间改变状态 | 执行入口及每次 Meta POST 前重检，返回 `preview_stale`/`rule_group_not_active`，变化后的对象不写 Meta | P0 | 通过（代码评审/自动化回归） |
| TC-026 | Enable TOCTOU | Token 校验期间规则配置被并发修改 | 启用规则组 | 最终事务重读行为字段和 preview hash，不一致返回 `preview_stale`，规则组保持禁用 | P0 | 通过（代码评审/自动化回归） |
| TC-027 | Legacy 配置资源 owner 隔离 | 两个用户及同 ID rule/account-group/rule-set | 列表、读取、保存、启停、删除 | 只能访问本人资源，跨用户 ID 返回 not found/forbidden 且无写入 | P0 | 通过（自动化） |
| TC-028 | Observe pause 零 Token | observe 组命中 pause | 正式调用 execute（非 dry-run） | 直接记录 `would_pause`，Token 配置/Token/Graph 均 0 次 | P0 | 通过（自动化） |
| TC-029 | Mixed copy/pause 隔离 | live 组同时含 pause 与 copy 候选 | 确认执行规则组 | copy 在 Token 前记 `copy_persistence_not_configured`；pause 继续成功，不被复制熔断连带阻断 | P0 | 通过（自动化） |
| TC-030 | Ownerless legacy fail-close | legacy 组的 `owner_user_id`/`created_by` 均为空且 enabled | 连续两次执行 schema ensure | 首次将其 `enabled=0, emergency_stopped=1`，第二次状态不变；ownerless standalone rule 也禁用 | P0 | 通过（自动化） |
| TC-031 | V2 不误迁移 | 已存在 `product=''` 的账号维度 V2 组 | 重复执行 schema ensure | 不被分类为 legacy，`run_mode`/owner/状态不被重写 | P0 | 通过（自动化） |
| TC-032 | Save 不可绕过 enabled | 新组或已禁用组 | 调用保存 API 并携带 `enabled=true` | 保存成功但仍 disabled；只有专用 enabled API 在 preview/确认均有效时才能开启 | P0 | 通过（自动化） |
| TC-033 | 损坏 preview 过期时间 fail-close | preview 记录存在，`expires_at` 为非法格式 | 读取/执行该 preview | 返回 `preview_invalid`，不得将其视为永不过期，Meta 写 0 次 | P0 | 通过（自动化） |
| TC-034 | 部署补丁兼容/幂等 | 当前 merged app 及可缺少旧函数/源码块的测试基线 | 执行 `--check`、apply、再次 apply | 已对齐 merged app 首次即 unchanged/零备份；需变更基线首次生成字节一致备份；可选目标缺失时安全跳过；第二次均幂等且不产生半成品 | P0 | 通过（自动化/本地补丁链验证） |
| TC-035 | 最终 pause 锁边界 | 正式 pause 已通过 preview | Graph GET/POST 慢响应或超时 | 锁内每次 POST 前仍能阻断 stale preview；同时记录全局 `JOB_DB_LOCK` 最坏约 60 秒 SQLite 写阻塞的 P2 生产监控项 | P2 | 通过（安全回归），生产性能待暗发布 |
| TC-036 | 账号维度 mixed 执行不回退产品白名单 | `product=''` 的 V2 组同时有 copy/pause 候选 | 执行 mixed 组并 spy 候选/白名单查询 | copy 在 Token 前隔离，pause 仅按 preview 账号归属重检，不再查 product/account 白名单、不把账号组退化成旧产品维度 | P0 | 通过（自动化） |
| TC-037 | Current-live action-log 兼容门禁 | 当前线上 `app.py` 的只读 fixture | 先执行 `--check`，再复制到临时目录 apply 两次并比较函数 hash | 原 fixture 字节不变；首次临时 apply 仅产生一份字节一致备份；二次 check/apply 均 `unchanged`；writer 63353、reader 63350、3/5 秒超时、`AD_CONTROL_LIVE_MAX_WORKERS=4`、无立即 upsert 重试和 7 个线上安全函数 hash 全部保留 | P0 | 通过（自动化/本地 fixture 验证） |
| TC-038 | 急停赢过并发启用 | live 组已有有效 preview，启用在锁外校验 Token | Token 校验期间分别触发同组和当前用户全局急停 | 最终启用返回 `emergency_stop_changed`，组保持 disabled+急停；不得清除并发急停 | P0 | 通过（自动化） |
| TC-039 | 旧急停显式恢复 | 启用请求开始前组已急停且 preview/Token 有效 | 无新急停时启用；另在校验期间再次急停 | 前者允许显式恢复，后者仍返回 `emergency_stop_changed` | P0 | 通过（自动化） |
| TC-040 | Legacy save 不可启用 | 已禁用 product/legacy 规则组 | 普通保存携带 `enabled=true`；另编辑已启用组行为 | 前者仍 disabled；后者行为变化强制 disabled 并失效 preview，均需专用启用接口 | P0 | 通过（自动化） |
| TC-041 | Legacy 账户池 owner 隔离 | 两个用户各有账户池，另有历史服务创建的同创建者绑定 | 保存/读取规则组并引用账户池 | 外部用户账户池引用失败；仅同 owner 或同 `created_by` 的现有服务绑定兼容，不泄漏账号 | P0 | 通过（自动化） |
| TC-042 | 日志目标明细 owner 隔离 | 两个 owner 各有 action/target | 调用 target 函数和 HTTP 明细路由 | 只返回本人目标；跨 owner 返回 not found/forbidden；路由显式传递当前 owner | P0 | 通过（自动化） |
| TC-043 | 账号规则日志可见 | 存在 `product=''` 的 V2 action log | 打开日志页默认筛选并加载绑定 | 默认显示“全部产品（含账号规则）”，账号规则及其本人绑定可见，不扩大跨 owner 范围 | P1 | 通过（自动化/静态契约） |
| TC-044 | 静态资源缓存版本一致 | 七个 ad-control HTML 页面 | 检查 CSS/JS URL | 全部使用 `v=20260715copy2`，不存在本次旧 cache buster | P1 | 通过（自动化） |
| TC-045 | Legacy standalone 生产基线 | 线上 SQLite `ad_control_rule` | 发布前、overlay 演练后、发布后只读比较 total/enabled 集合 | 三次结果一致；不改变其 grandfather 启停/急停语义，不读取 Token | P0 | 前置基线通过（0/0）；overlay/发布后待验证 |
| TC-046 | 补丁后观察日志语义 | `criteria.run_mode=observe` 的 action，current-live/旧基线临时副本 | 应用真实部署补丁，分别读取日志列表和 target 明细并重跑日志/API测试 | 两条路径均为 `audit.mode=observe`、`mode_label=只观察`、状态 `observed/观察完成`；不得回退成 real/正式执行 | P0 | 通过（部署补丁全量回归） |

## 执行结果说明

2026-07-15 使用独立 `PYTHONPYCACHEPREFIX` 执行 `python -m unittest discover -s tests -v`，结果 `Ran 107 tests`、`OK`。另执行 `python tests/validate_ad_control_deploy_patch.py`，在当前 merged app 临时副本上确认首次/二次 apply 均 `unchanged`、零备份、字节不变并重跑全量通过；若输入需要变更，该验证器仍要求首次产生唯一字节一致备份。再用 `tests/validate_ad_control_live_action_log_compat.py --live-app <current-live-app.py>` 验证真实 current-live 基线的首次 changed+备份 hash、二次幂等和 action-log writer/reader 安全契约。所有“通过”均为本地自动化、隔离测试、本地 fixture/补丁链验证或静态审查结果；尚未完成生产 overlay 全量验证、GitHub-first 暗发布或真实线上 smoke test，未调用真实 Meta copy，也未开放 Ad 执行。

## 回归范围

- `/ad-control.html` 导航和模块权限。
- 规则组列表、编辑、删除、启停、立即试算和日志页。
- Token 与账号池现有读取。
- 现有 Campaign pause 执行及执行日志持久化。
- runner 锁、续批、资源熔断、cron 兼容。
- 本期不包含复制后 created_data 再扫描，也不建/写 copied created_data、copy lineage 或 copy intent；DDL 环境已修复不改变本轮范围，该能力随用户后续明确授权的复制结果 ads_ai 写入方案实现。既有 `ads_ai.ad_control_action_log` 仅是执行审计。
