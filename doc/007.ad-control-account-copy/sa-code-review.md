# SA 代码评审

## 结论

2026-07-15 最终冻结代码评审与生产验收通过，BUG-008 修复已完成 Python/JavaScript 静态检查、180 条 ad-control fresh-cache 自动化测试、exact-source staging/overlay、SQLite owner 迁移、生产 smoke及四轮自然 runner tick。当前生产运行代码为 `375185d5c7ad8dbdf39eae8e5c8b8ddf7a45b9a5`，`app.py` SHA-256 为 `7ed60179abc83880d41f2547ed19e3591136dca693c776df5d5ecfe6a2546b49`，runner SHA-256 为 `a3fa7b2bbe597e52dec44347de750fe34d313302baefba585f66d521dd5c25e7`。

该结论只覆盖本期实际开放边界：Campaign 规则配置、观察/试算、runner 观察链路及既有 Campaign pause 回归。真实 Meta Campaign copy 因复制结果持久化未配置而在任何 Token/Graph 访问前失败关闭；Ad 仅允许保存配置，启用、候选、试算、runner 和正式执行均未开放。本次没有执行真实 Meta copy Canary。

## 评审范围

- `app.py`：账号级规则组 API、本人数据隔离、旧聚合组原子迁移、preview/enable 并发重检、Campaign 观察/试算、正式 copy 前置熔断、Ad 顶层阶段熔断。
- `features/ad_control_copy_engine/`：规则归一化、冲突消解、计划/额度/冷却和隔离的 Meta copy 编排契约。
- `scripts/ad_control_rule_runner.py`：规则组观察执行、`would_pause`/`would_copy` 汇总、campaign-start schema lazy singleflight、no-due 零 schema I/O 及既有 runner 状态回归。
- `static/ad-control-pages.js`、`static/ad-control-rules.html`、`static/ad-control-pages.css`：去产品维度、对象层级/运行模式拆分、复制参数、旧规则组兼容及页面缓存版本。
- `features/ad_control_execution_log/`、`deploy/apply_ad_control_execution_log_fix.py`：既有 `ads_ai.ad_control_action_log` 审计兼容与权限回归，包含线上新版 writer/reader 分离、超时/并发上限及无立即 upsert 重试的保护。
- `tests/test_ad_control*.py` 及同域发布安全测试：共 180 条规则模型、API、UI、runner、245 账号池、跨 owner 缓存隔离、daily/raw 执行日志、空白名单/无到期账号、schema critical retry/singleflight 回归、部署补丁兼容、exact-source 发布器、SQLite owner 迁移器和隔离编排测试。
- `deploy/apply_ad_control_account_copy_v2.py`：生产共享 monolith 的 exact-source Git diff、target blob、唯一备份与原子替换门禁。
- `tests/validate_ad_control_deploy_patch.py`：将部署补丁真实应用到当前 merged app 的临时副本；若已对齐则首次即 `unchanged` 且零备份，若需变更则校验一份字节一致写前备份；两种情况的二次 apply 均须 `unchanged` 且不新增备份，并对临时 app 重跑同一全量测试。当前线上旧基线的真实 changed+backup 路径由下一项 current-live validator 独立证明。
- `tests/validate_ad_control_live_action_log_compat.py`：对当前线上 `app.py` 只读 fixture 执行 check，只在临时副本 apply，验证写前备份、二次幂等以及 7 个线上 action-log 安全函数 hash 不变。

不在本次评审放行范围：复制结果 copied created_data/lineage/intent 的 `ads_ai` 建表与写入、真实 Meta Campaign copy、全部 Ad 扫描/执行链路。DDL 环境问题已修复不代表本轮获得了建表/写入授权。既有 `ads_ai.ad_control_action_log` 不属于上述延后范围，仍按原审计链路保留，但它不是复制结果落表。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | `app.py` 正式 copy 执行入口 | 未接入复制结果持久化时不得先读 Token 或调用 Meta，否则可能形成无落表对象 | 在 Token/Graph 访问前固定返回 `copy_persistence_not_configured` | 已修复 |
| CR-002 | P0 | `app.py`、runner、规则组 UI | Ad 第二阶段不可复用 Campaign 候选或被误启用 | 保存配置以外的全部 Ad 入口统一返回 `phase_not_enabled` | 已修复 |
| CR-003 / BUG-001 | P1 | `app.py`、`static/ad-control-pages.js` | 去产品维度后，旧 fan-out 聚合规则组可能被误报为已禁用，编辑保存缺少安全收敛 | 显示 `partial_enabled`，显式提交迁移源并在同一事务内校验、保存、禁用和软删除旧行 | 已修复 |
| CR-004 | P1 | API/UI 兼容层 | 旧 `action=observe` 与未知 action 若静默按 pause 处理会改变语义 | 旧 observe 显式迁移为观察模式 + pause；未知 action 拒绝保存 | 已修复 |
| CR-005 | P1 | 执行日志查询 | MySQL 权限错误不得被 SQLite fallback 吞掉，且删除后的本人规则组仍需保留历史审计 | 保留结构化权限错误并按 owner/binding 分页过滤日志 | 已修复 |
| CR-006 / BUG-002 | P0 | `app.py` preview/execute | 规则修改、禁用或急停后，旧 preview 仍可能继续触发 Meta pause；多对象执行期间状态变化也可能越过旧检查 | 执行入口绑定当前规则 hash/最后 preview/owner/active 状态，并在每次 Meta POST 前持 SQLite 写锁再次校验 | 已修复 |
| CR-007 / BUG-002 | P0 | `app.py` enabled 切换 | Token 校验在锁外执行，校验结束到 enabled 写入之间存在 TOCTOU，可能启用另一份已变化配置 | `BEGIN IMMEDIATE` 后重读全部行为字段、Ad 门禁和 preview hash，不一致返回 `preview_stale` | 已修复 |
| CR-008 | P0 | legacy rule/account-group/rule-set API | 旧配置资源接口缺少完整 owner 过滤与写前校验，存在跨用户读写 ID 的可能 | 列表、读取、保存、启停和删除全部绑定当前 owner；外部调用不得使用 internal 绕过 | 已修复 |
| CR-009 | P0 | `execute_ad_control_live` | Ad 仅在候选阶段短路仍不够，构造旧 preview 可能进入 Campaign 执行分支 | 解析 preview 后在顶层立即检查 `object_level=ad` 并返回 `phase_not_enabled` | 已修复 |
| CR-010 | P0 | observe pause 执行 | 观察模式 pause 若先解析 Token，会产生不必要外部依赖和潜在副作用 | 仅正式 pause 解析 Token；observe pause/copy 均直接记录 `would_*` | 已修复 |
| CR-011 | P0 | mixed pause/copy 规则组 | 未配置复制落表时，copy 熔断若在整组级处理会同时阻断既有 pause | 在任何 Token 访问前逐对象隔离 copy 并记录 `copy_persistence_not_configured`，其余 pause 继续正常执行 | 已修复 |
| CR-012 | P2 | `app.py` 时间处理 | 测试输出出现 `datetime.utcnow()` 弃用告警 | 后续技术债改为 timezone-aware UTC；当前不影响测试与运行结果 | 待后续优化 |
| CR-013 | P0 | SQLite legacy schema ensure | owner/created_by 双空的启用组无人可管理，但可被 internal runner 执行 | 迁移时自动 `enabled=0, emergency_stopped=1`；生产只为已核实主体精确赋 owner | 已修复 |
| CR-014 | P0 | SQLite legacy 识别 | 空 product 的账号维度 V2 组可能被重复归类成 legacy | 只迁移 `product <> ''` 的历史绑定，重复 ensure 保持 V2 不变 | 已修复 |
| CR-015 | P0 | 规则组 save | payload 中的 `enabled=true` 可能绕过 preview/Token/二次确认的专用启用流程 | normalize/save 只保留数据库现有 enabled，新组强制 disabled；行为变更禁用并失效 preview | 已修复 |
| CR-016 | P0 | preview 过期检查 | 非法 `expires_at` 原先可能被当作不过期，导致损坏 preview 继续可用 | 解析异常返回 `preview_invalid`，执行 fail-closed | 已修复 |
| CR-017 | P0 | `deploy/apply_ad_control_execution_log_fix.py` | 旧基线缺少可选函数/源码块时补丁 `--check` 中断，无法形成可回滚发布链 | 可选目标安全跳过；当前 merged app 与有/无 legacy 目标均验证 check/apply/幂等/写前备份 | 已修复 |
| CR-018 | P2 | `ad_control_guarded_campaign_pause` | 为保证每次 Graph POST 前 preview 仍有效，全局 `JOB_DB_LOCK` + `BEGIN IMMEDIATE` 跨越 Graph GET/POST，最坏可阻塞其他 SQLite 写约 60 秒/对象 | 本期保留一致性优先实现；暗发布监控 API/runner 耗时、Graph 超时与 job 写入排队，异常时停 runner/live 组后回滚代码 | 已接受，生产待验证 |
| CR-019 | P0 | 部署模板 mixed 执行 | 账号维度 V2 组若回退调用旧产品/账号白名单，会在 `product=''` 时错误过滤 pause 候选；模板对主 app audit helper 的隐式依赖也可导致旧基线 `NameError` | 补丁模板自包含 audit 依赖；copy 对象预先 fail-closed，pause 只按 preview 中已验证账号重检，不再回查 product/account 白名单 | 已修复 |
| CR-020 | P0 | action-log 部署补丁 | 当前线上版本已有安全性更新；仅对仓库旧基线测试会漏掉 writer/reader、3/5 秒超时、live worker=4 和无立即 upsert 重试被旧模板回退的风险 | 补丁内置安全契约断言，新增 current-live fixture 只读/check/临时 apply/二次幂等验证，对 7 个已有线上安全函数做 hash 不变断言 | 已修复，每次部署前必须重跑 |
| CR-021 | P0 | 专用 enabled 接口 | Token 校验在锁外时，同组或全局急停可能先完成，旧实现随后无条件清除急停并启用 | 记录启用开始时 emergency 状态/版本；最终事务比较 `updated_at`，新急停返回 `emergency_stop_changed`，只有无新急停的显式恢复可清除旧状态 | 已修复 |
| CR-022 | P0 | legacy/product 规则组普通 save | 旧保存入口可读取 payload `enabled=true`，绕过 preview、Token 和专用启用接口 | save 不接受 0→1；仅保留未改行为的既有 enabled 状态，任何行为变化强制禁用并失效 preview | 已修复 |
| CR-023 | P1 | legacy 账户池关联 | 规则组可引用其他 owner 的账户池，候选解析时可能带入对方账号 | 写前事务校验 owner；历史服务绑定只在同 `created_by` 下兼容，跨 owner 引用 fail-closed | 已修复 |
| CR-024 | P0 | action target 明细 | HTTP 路由未向目标查询传 current owner，缓存/详情存在越权风险 | 函数与路由强制 owner，并在任何缓存读取前先 fetch action 完成 owner 校验；跨 owner 返回 not found/forbidden | 已修复 |
| CR-025 | P1 | 日志页与静态缓存 | 默认产品过滤遗漏 `product=''` 账号规则，且页面 cache buster 不一致会继续运行旧 UI | 默认文案/查询包含账号规则；合并 daily/raw UI 后七个 ad-control 页面统一 `v=20260715copylog3` | 已修复 |
| CR-026 | P2/兼容边界 | legacy standalone `ad_control_rule` | 其保存可直接启用，且不受 rule-group 全局急停覆盖 | 本期 grandfather 保留既有行为；部署前后只读核实 enabled 基线并记录，禁止把 V2 急停表述为全局覆盖 standalone | 已接受；发布前后 total/enabled=0/0 |
| CR-027 / BUG-003 | P0 | action-log 列表/目标明细与部署补丁模板 | 列表模板应用后把 `observe/只观察` 回退成 `real/正式执行`，而 target 明细的 `ad_control_action_audit` 本身也缺 observe 分支 | 列表和明细都读取 `criteria.run_mode` 并生成 observed 状态；current-live validator 分别抽取两函数验证；补丁后重跑 107 条 | 已修复 |
| CR-028 / BUG-004 | P0 | V2 生产 `app.py` 部署链 | execution-log 兼容补丁只覆盖日志函数，不能生成完整 V2 app；误用会形成半发布 | 新增 exact-source Git diff 发布器：临时 apply 后核对 target blob，source 漂移写前失败，唯一字节备份后原子替换 | 已修复并通过生产 staging/overlay |
| CR-029 / BUG-005 | P1 | live mixed 候选批次 | 已知会熔断的 copy 与 pause 一起参与每账号上限，可能占用关闭名额并让 runner 不续批 | live 尚有 pause 时只对 pause 做公平分批，保留 mixed 总数/确认语义；真实 21 pause + 1 copy preview→execute 与 continuation 回归 | 已修复 |
| CR-030 | P0 | live SQLite owner 迁移 | app 启动即 ensure 会先把历史 owner 回填为 `codex`；手工 SQL/错误DB/部分更新会造成当前用户不可见或错误归属 | 新增默认 dry-run 的 exact-state 迁移器，显式DB/app/三组状态，事务首跑3/幂等0及全不变量断言；全同库 writer 停止后才允许 apply | 已修复；生产 check/3/0 通过 |
| CR-031 / BUG-006 | P0 | legacy 产品账号列表 owner 贯穿/缓存 | owner 隔离后 saved pool 读取漏传 actor，冷缓存直接 `missing_owner`；若只放宽校验，按 product 共用缓存会污染不同用户的 saved-only 账号 | route→列表→legacy loader→saved pool 显式传 owner，缓存键改为 `(owner, product)`；新增 245 账号、fallback、并发和双 owner 隔离回归 | 已修复 |
| CR-032 | P2 | legacy 产品账号列表缓存失效 | 保存/删除账户池后不会主动清理 300 秒 TTL 缓存，同一 owner 可能短暂看到本人旧账号列表 | 保持旧接口 TTL 兼容；缓存已按 owner 隔离，不构成越权；V2 无产品列表不使用该缓存，后续单独优化主动失效 | 已接受 |
| CR-033 / BUG-007 | P0 | 空 Campaign 白名单与账号时区自然 tick | V2 在排期前把空白名单改为错误；无账号到期时仍形成 `live_preview_blocked` 并写 action 审计 | 空白名单在排期/Token/Graph 前作为零候选返回；preview 汇总 `scheduled_due_count`；无到期、无错误、无目标时返回 `skipped/no_accounts_due` 且不写 action；错误总数不受100条明细截断 | 已修复；174/174、隔离生产数据演练及19:25自然 tick通过 |
| CR-034 / BUG-008 | P1 | campaign-start schema 校验与 runner tick | `SHOW COLUMNS` 瞬时读取失败被 best-effort helper 转为空集合并误报真实缺列；runner 在无到期账号时也无条件探测 schema | schema 校验改用 critical retry；持续读取失败报 `insight_start_schema_unavailable`，读取成功后真实缺列才报 `invalid_insight_start_schema`；每次规则组事件lazy singleflight、各 worker 独立异常、`no_accounts_due` 零 schema I/O | 已修复；180/180及四轮生产自然tick通过 |

## 编译 / 验证结果

| 验证项 | 命令 | 结果 |
| --- | --- | --- |
| Python 编译 | `python -m py_compile app.py scripts/ad_control_rule_runner.py features/ad_control_copy_engine/service.py features/ad_control_execution_log/service.py deploy/apply_ad_control_execution_log_fix.py deploy/apply_ad_control_account_copy_v2.py deploy/migrate_ad_control_account_copy_v2_sqlite.py` | 通过，7/7，退出码 0 |
| JavaScript 语法 | `node --check static/ad-control-pages.js` | 通过，退出码 0 |
| ad-control fresh-cache 全量测试 | 独立 `PYTHONPYCACHEPREFIX` + `python -m unittest discover -s tests -p "test_ad_control*.py" -v` | 180/180 通过，0 失败，0 阻塞 |
| Exact-source app 合并器 | `python -m unittest tests.test_ad_control_account_copy_deploy -v` | 12/12 通过；共享锁、未知源/漂移阻断、持久化唯一备份、安装失败恢复、二次幂等 |
| SQLite owner 迁移器 | `python -m unittest tests.test_ad_control_account_copy_sqlite_migration -v` | 8/8 通过；常量门禁、dry-run零写、首跑3/幂等0、pool/rule-set/状态不变量、TOCTOU/触发器事务回滚、真实 target app 路由与 owner 可见性 |
| 真实部署补丁链 | `python tests/validate_ad_control_deploy_patch.py` | 当前 merged app 首次 apply `unchanged`、零备份且字节不变；二次 apply 仍 `unchanged`、不新增备份；临时 app 全量通过 |
| Current-live action-log 兼容 | `python tests/validate_ad_control_live_action_log_compat.py --live-app <current-live-app.py>` | 原 fixture 只读；临时 apply 首次备份 hash 匹配；二次 check/apply `unchanged`；writer 63353、reader 63350、3/5 秒超时、live worker=4 及 7 个安全函数 hash 保留 |
| 差异格式检查 | `git diff --check` | 通过，退出码 0 |

新增安全回归覆盖 stale/损坏 preview、执行前及每次 Meta POST 前重检、enable TOCTOU 与急停竞态、V2/legacy save-enabled 绕过阻断、ownerless legacy fail-close、V2 不误迁移、legacy 配置资源及账户池 owner 隔离、action target owner 传递、账号规则日志可见性、统一 cache buster、Ad execute 顶层门禁、observe pause 零 Token、mixed copy/pause 隔离、空 Campaign 白名单在排期/Token/Graph 前零调用返回、无到期账号零 action 审计、schema 查询瞬时/持续失败和真实缺列语义、同一规则组事件并发singleflight与独立异常实例、preview 错误总数不随明细截断，以及部署补丁后 observe 审计语义、旧基线、先前泛化 action-log 补丁输出和 current-live writer/reader 安全契约的兼容/幂等校验。

本地和 staging 验证使用临时 SQLite、fake/stub、静态契约或当前线上文件只读副本；生产发布仅执行 exact-source overlay、精确 owner 迁移与只读/smoke 验收。18:50 首次自然 tick 只生成既有 action 审计且 requested/success=0/0；19:25 BUG-007 热修 tick 返回 `skipped/no_accounts_due` 且 action 数量前后保持17。19:40 BUG-008 tick 因 `SHOW COLUMNS` 瞬时失败安全结束，无 preview/action、零 Meta 写；19:45 与后续旧版本自然恢复。当前 `375185d` 在 20:11:18 恢复 cron 前基线 action=17、preview=52、最新对象状态=04:22:58，API/worker/crond active、auth=200、playable unauth=403、public page=200；20:15至20:30四轮自然tick均为 `skipped/no_accounts_due`，preview 52→56、action=17、对象状态不变且日志零schema probe/Traceback。全程没有调用 Meta copy，没有建或写 copied created_data/lineage/intent。`ads_ai.ad_control_action_log` 属于既有审计链路，不代表复制结果落表已实现。
