# 测试报告

## 测试结论

2026-07-15 最终本地冻结验证通过：合并 16:01 daily-log 生产组合并收口 mixed 批次、exact deploy 与 owner 迁移门禁后，Python 编译与 `node --check` 通过，ad-control 使用独立 Python 缓存的全量自动化测试 161/161 通过，exact-source app 合并器 12/12、SQLite owner 迁移器 8/8 通过，`git diff --check` 通过。当前未发现未关闭的 P0/P1 缺陷。

本报告证明的是当前工作树在本期安全边界内通过测试，不是生产部署证明。测试未调用真实 Meta 写接口，未开放真实 Campaign copy 或任何 Ad 执行能力，生产 overlay 完整验证、GitHub-first 暗发布、服务重启、定时任务切换和线上 Canary 均尚未完成。

## 测试范围

- 规则组本人隔离、账号维度保存、默认禁用/观察模式和正式模式二次确认。
- 关闭/复制动作与观察/正式运行模式分离，pause 优先和 copy shadowed 规则。
- Campaign 观察/试算、Top N、剧目范围、时区计划、额度/冷却及 runner `would_*` 记录。
- Campaign 正式 copy 在 Token/Graph 访问前返回 `copy_persistence_not_configured`。
- stale preview 在执行入口和每次 Meta POST 前重新绑定当前规则 hash、最后 preview、owner、enabled/急停状态；enable 在 Token 校验后进行事务内 TOCTOU 重检。
- 非法 preview `expires_at` 返回 `preview_invalid` 并 fail-closed；普通 save 携带 `enabled=true` 也不能绕过专用启用流程。
- observe pause/copy 均不读取 Token；mixed 规则组先隔离 copy，既有 pause 不被复制熔断连带阻断。
- Ad 只允许保存配置；启用、候选、试算、runner 和正式执行的顶层入口均返回 `phase_not_enabled`。
- 旧 fan-out 聚合规则组原子迁移、`partial_enabled`、旧 observe 动作迁移、未知动作拒绝，以及 legacy rule/account-group/rule-set owner 隔离。
- owner/created_by 双空 legacy 组禁用+急停，以及 `product=''` 的账号维度 V2 组在重复 ensure 时不误迁移。
- 既有 Campaign pause、执行日志、全局熔断、批次限制和 runner 状态回归。
- 部署补丁在当前 merged app 和缺失可选 legacy 目标的旧基线上均能完成 check/apply/幂等判定；当前 merged app 已对齐时首次为 `unchanged` 且不生成冗余备份，确需变更的基线会在首次写入前生成唯一、字节一致的备份。
- 部署模板自包含执行审计依赖；账号维度 mixed copy/pause 不会回退调用旧 product/account 白名单而误过滤 pause。
- current-live fixture 只读验证：线上既有 writer 63353、reader 63350、3/5 秒超时、`AD_CONTROL_LIVE_MAX_WORKERS=4`、runner 状态更新不立即 upsert 重试和 7 个 action-log 安全函数 hash 均未被补丁回退；临时副本二次 check/apply 均为 `unchanged`。
- 隔离 copy engine 的 CBO/ABO、轮询、映射、幂等、临时 intent/lineage 契约；这些用例使用 fake/stub 与临时 SQLite，不代表生产复制链路已放开。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python 编译文件 | 7 | 7 | 0 | 0 |
| JavaScript 语法文件 | 1 | 1 | 0 | 0 |
| ad-control 自动化用例 | 161 | 161 | 0 | 0 |
| Exact-source app 合并器 | 12 | 12 | 0 | 0 |
| SQLite owner 迁移器 | 8 | 8 | 0 | 0 |
| 真实部署补丁链 | 1 | 1 | 0 | 0 |
| Current-live action-log fixture | 1 | 1 | 0 | 0 |
| 服务器恢复后只读基线 | 1 | 1 | 0 | 0 |
| Git 差异格式检查 | 1 | 1 | 0 | 0 |

## 缺陷情况

- `BUG-001`：旧 fan-out 聚合规则组编辑迁移与部分启用状态兼容问题，已修复并回归通过。
- `BUG-002`：旧 preview/enable TOCTOU 可能越过最新规则状态的问题，已修复并回归通过。
- `BUG-003`：部署补丁模板及 target 明细审计会把观察日志误标成正式执行，已修复列表/明细两条路径并在补丁后临时 app 上 107/107 回归通过。
- `BUG-004`：execution-log 补丁不能生成完整 V2 app，已新增 source/target Git blob、隔离 diff、唯一备份和原子替换门禁；生产 staging 尚待验证。
- `BUG-005`：deferred copy 会占用同账号 pause 批次额度并可能终止续批，已改为 pause 优先分批，并通过 21 pause + 1 copy 的真实 preview→execute/runner continuation 回归。
- 当前无未关闭 P0/P1 缺陷。
- 存在非阻断技术债：`app.py` 的 `datetime.utcnow()` 在测试中产生弃用告警，未影响 161 条测试结果。
- 存在已接受的 P2 运行取舍：正式 Campaign pause 的最终一致性检查持有全局 `JOB_DB_LOCK` + SQLite `BEGIN IMMEDIATE` 跨 Graph GET/POST，两次 30 秒超时时可使同进程其他 job SQLite 写阻塞约 60 秒/对象。安全回归已通过，生产性能影响必须在暗发布中观察。

## 验证证据

```text
python -m py_compile app.py scripts/ad_control_rule_runner.py features/ad_control_copy_engine/service.py features/ad_control_execution_log/service.py deploy/apply_ad_control_execution_log_fix.py deploy/apply_ad_control_account_copy_v2.py deploy/migrate_ad_control_account_copy_v2_sqlite.py
结果：退出码 0

node --check static/ad-control-pages.js
结果：退出码 0

独立 PYTHONPYCACHEPREFIX + python -m unittest discover -s tests -p "test_ad_control*.py" -v
结果：Ran 161 tests / OK / 退出码 0

python -m unittest tests.test_ad_control_account_copy_deploy -v
结果：Ran 12 tests / OK；check 零写、共享锁、source/备份后漂移阻断、持久化唯一备份、mode-before-fsync、安装失败恢复、未知字节不覆盖、二次幂等 / 退出码 0

python -m unittest tests.test_ad_control_account_copy_sqlite_migration -v
结果：Ran 8 tests / OK；dry-run 原库零写、逐ID/product/state常量门禁、首跑3/幂等0、mixed owner/坏pool/TOCTOU失败关闭、触发器副作用事务内回滚、真实 target app ensure/owner 可见性集成 / 退出码 0

python tests/validate_ad_control_deploy_patch.py
结果：当前 merged app 临时副本首次/二次均 app.py unchanged、零备份且字节不变；补丁后全量测试 OK / 退出码 0。需要变更的 current-live 首次 changed+备份路径由下一项验证。

python tests/validate_ad_control_live_action_log_compat.py --live-app <current-live-app.py>
结果：上一轮 `83d3cc...` fixture 的 check/apply/幂等和 7 个安全函数验证通过；16:01 daily-log 发布后的当前 live SHA-256 为 `c5f5be0f4342b262fe9adb1513770a779e684b45500efc31c5d12b1501ca072e`，须在 production staging 中重新执行同一验证后才能作为本轮发布证据。

git diff --check
结果：退出码 0
```

关键安全断言已覆盖：观察模式 pause/copy 零 Token/Graph 访问；Campaign copy 在 Token/Graph 前失败关闭；mixed 中 copy 提前隔离且 pause 可继续成功，账号维度 mixed 不回查旧 product/account 白名单；Ad execute 顶层短路；stale/损坏 preview 失败关闭，执行前及每次 Meta POST 前重检；enable TOCTOU、同组/全局急停竞态与 V2/legacy save-enabled 绕过均被阻断；ownerless legacy 自动收敛；V2 不误迁移；legacy rule/account-group/rule-set/账户池 owner 隔离；action target 明细 owner 传递；账号规则日志可见和七页 cache buster 一致；旧聚合组迁移失败时完整回滚；未知 action 不会静默转成 pause；部署补丁自包含 audit，对旧基线、泛化 action-log 旧补丁输出和 current-live writer/reader 安全契约均能安全升级且重复应用幂等。

## 遗留风险

- copied created_data/lineage/intent 的 `ads_ai` 分渠道表结构、事务写入和回流扫描尚未设计与实现；本轮不建表、不写入。DDL 环境问题已修复只表示后续获得用户明确授权后可再尝试，因此真实 Meta Campaign copy 必须继续保持关闭。
- 既有 `ads_ai.ad_control_action_log` 是执行审计链路，不是 copied created_data/lineage/intent 或复制结果落表；本次已做代码与 current-live fixture 兼容回归，但未在生产 MySQL 上执行写入验证。
- Ad 第二阶段只有配置契约，没有真实候选扫描、复制、落表或端到端验证。
- Meta copy 编排测试使用 fake/stub；未验证真实 Graph API 版本、账号权限、异步 copy 完成时间、CBO/ABO 实际返回形态和限流行为。
- C0 备份已完成，但生产 overlay 的生产 Python/依赖环境完整测试、GitHub exact-commit 拉取、C1 发布前备份、服务重启、环境变量读回、日志观察、暗发布和线上 smoke test 均尚未完成。
- `JOB_DB_LOCK` 跨 Graph 请求的实际负载影响尚无生产证据；上线后须监控 API/runner 耗时、Graph 超时和 job SQLite 写入排队，异常时先停 ad-control runner/禁用受影响 live 组再回滚。
- `datetime.utcnow()` 弃用告警应在后续技术债中改为 timezone-aware UTC。
- legacy standalone `ad_control_rule` 本期按兼容边界保留直接保存/启停，且不受 rule-group 全局急停覆盖。服务器恢复后只读基线 total/enabled=0/0；overlay 与发布后必须继续为 0/0，该边界不属于 V2 规则组能力。
- 服务器恢复后又发现 16:01 daily-log 并发发布；当前生产关键文件精确对应组合提交 `0a4c408eb7d027eb60eb15496c6dae48443a2a1c`（daily-log 源提交 `d4af68af83e55b4df65fc13f273738ba98dfe189`），旧 `146cb1b` 不能再作为直接部署 source。exact-source 发布器必须以重新核验的当前 live blob 为门禁，任何漂移立即停止。

## 发布建议

代码质量检查、旧基线补丁链与 current-live fixture 兼容验证通过，可进入生产 overlay 完整验证、GitHub-first 提交和暗发布审批；正式部署前必须对刚刷新的当前线上副本重跑 current-live validator，当前不得宣称功能已上线。

若后续部署本期版本，应先完成可回滚备份，确认复制总熔断关闭、Ad 阶段熔断生效，只开放 Campaign observe/preview 与既有 pause，再进行只读/观察 smoke test。真实 Campaign copy 必须等待 copied created_data/lineage/intent 落表方案、事务契约和回归测试完成后另行评审；Ad 复制继续作为第二阶段单独验收。
