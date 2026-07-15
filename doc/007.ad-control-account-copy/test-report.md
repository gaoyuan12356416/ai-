# 测试报告

## 测试结论

2026-07-15 本地冻结、生产 staging/overlay、服务/API smoke与四轮自然 runner tick 均已通过：ad-control fresh-cache 全量自动化测试 180/180、exact-source app 合并器 12/12、SQLite owner 迁移器 8/8、生产 Python/JavaScript 检查、playable preview 回归与真实浏览器均通过。当前生产运行代码提交为 `375185d5c7ad8dbdf39eae8e5c8b8ddf7a45b9a5`，`app.py` SHA-256 为 `7ed60179abc83880d41f2547ed19e3591136dca693c776df5d5ecfe6a2546b49`，runner SHA-256 为 `a3fa7b2bbe597e52dec44347de750fe34d313302baefba585f66d521dd5c25e7`。BUG-008 生产验收和本次最终发布验收已闭环。

本报告证明的是本期安全边界内的代码与生产部署已经验收，不代表真实 Meta Campaign copy 已开放。发布没有调用 Meta copy、没有建写 copied created_data/lineage/intent，也没有开放任何 Ad 执行能力；正式 Campaign copy 仍在 Token/Graph 前返回 `copy_persistence_not_configured`，后续真实复制 Canary 必须另行评审。

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
- 生产遗留的 245 账号池回归已纳入版本化测试：HTTP route 从 session 注入 owner 由静态契约断言覆盖，service 到 saved pool 由运行时测试覆盖；缓存按 `(owner, product)` 隔离，业务库失败 fallback、并发单次刷新及跨用户缓存不可见均通过。
- 空 Campaign 白名单在账号排期、Token、Graph 之前返回零候选；账号时区规则无账号到期时返回 `skipped/no_accounts_due`，不写 action 审计；preview 错误总数不受最多100条详情截断影响。
- Campaign start schema 查询的瞬时/持续失败和真实缺列语义分离：critical query 默认总共尝试4次（首次加3次重试，可由 `AD_CONTROL_CRITICAL_DB_RETRIES` 配置），持续读取失败返回 `insight_start_schema_unavailable`，成功读取但缺 `campaign_id/dt` 才返回 `invalid_insight_start_schema`；runner 在实际需要时按规则组事件lazy singleflight，各 worker 获得独立异常，`no_accounts_due` 为零 schema I/O。
- 隔离 copy engine 的 CBO/ABO、轮询、映射、幂等、临时 intent/lineage 契约；这些用例使用 fake/stub 与临时 SQLite，不代表生产复制链路已放开。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python 编译文件 | 7 | 7 | 0 | 0 |
| JavaScript 语法文件 | 1 | 1 | 0 | 0 |
| ad-control 自动化用例 | 180 | 180 | 0 | 0 |
| Exact-source app 合并器 | 12 | 12 | 0 | 0 |
| SQLite owner 迁移器 | 8 | 8 | 0 | 0 |
| 真实部署补丁链 | 1 | 1 | 0 | 0 |
| Current-live action-log fixture | 1 | 1 | 0 | 0 |
| 服务器恢复后只读基线 | 1 | 1 | 0 | 0 |
| 最终生产 staging / overlay / SQLite 迁移 | 1 | 1 | 0 | 0 |
| 生产浏览器验收 | 1 | 1 | 0 | 0 |
| 原 V2 首次自然 runner tick 业务语义 | 1 | 0 | 1（BUG-007，安全零写） | 0 |
| BUG-007 热修自然 runner tick | 1 | 1 | 0 | 0 |
| BUG-008 旧版本自然 runner tick 业务语义 | 1 | 0 | 1（瞬时 schema 读取失败被误报，安全零写） | 0 |
| BUG-008 修复自动化/部署 smoke | 1 | 1 | 0 | 0 |
| BUG-008 修复后生产自然 runner tick | 4 | 4 | 0 | 0 |
| Git 差异格式检查 | 1 | 1 | 0 | 0 |

## 缺陷情况

- `BUG-001`：旧 fan-out 聚合规则组编辑迁移与部分启用状态兼容问题，已修复并回归通过。
- `BUG-002`：旧 preview/enable TOCTOU 可能越过最新规则状态的问题，已修复并回归通过。
- `BUG-003`：部署补丁模板及 target 明细审计会把观察日志误标成正式执行，已修复列表/明细两条路径并在补丁后临时 app 上 107/107 回归通过。
- `BUG-004`：execution-log 补丁不能生成完整 V2 app，已新增 source/target Git blob、隔离 diff、唯一备份和原子替换门禁；最终生产 staging 与原子 overlay 已验证。
- `BUG-005`：deferred copy 会占用同账号 pause 批次额度并可能终止续批，已改为 pause 优先分批，并通过 21 pause + 1 copy 的真实 preview→execute/runner continuation 回归。
- `BUG-006`：owner 隔离改造后，legacy 产品账号列表未传当前用户且缓存仅按产品区分，缓存未命中会报 `missing_owner`，简单放宽后还可能形成跨用户缓存污染；现已贯穿当前 owner 并按 `(owner, product)` 隔离缓存，245 账号池、fallback、并发和双 owner 回归通过。
- `BUG-007`：空 Campaign 白名单在账号排期前被计为错误，导致首次自然 tick 形成 `live_preview_blocked/error_count=108`。该 tick requested/success=0/0、零 Token/Graph/Meta 写；现已将空白名单恢复为 benign 零候选，并在无到期账号时返回 `no_accounts_due`、不写 action。19:25 热修自然 tick 对账通过，action 17→17。
- `BUG-008`：19:40 旧版本执行 `SHOW COLUMNS` 遇到 MySQL client exit 1，best-effort helper 把读取失败返回空集合并误报真实缺列。该 tick 无 preview/action、requested/success/error=0/0/0、action保持17、对象状态未变化且零 Meta 写；19:41只读回查确认54列和必需字段完整，19:45与后续旧版本自然恢复。`375185d` 已实现 critical retry、读取失败/真实缺列语义分离、no-due 零 schema I/O、每次规则组事件singleflight和独立异常；自动化180/180、部署smoke及四轮生产自然tick均通过。
- 当前未发现新的代码级 P0/P1 缺陷；BUG-008 生产验收已闭环。
- 存在非阻断技术债：`app.py` 的 `datetime.utcnow()` 在测试中产生弃用告警，未影响 180 条测试结果。
- 存在已接受的 P2 兼容取舍：legacy 产品账号列表缓存仍为 300 秒 TTL，账户池保存/删除不会主动清除同一 owner 的旧缓存；最多短暂显示本人旧数据，不造成跨用户可见，V2 无产品列表分支不使用该缓存。
- 存在已接受的 P2 运行取舍：正式 Campaign pause 的最终一致性检查持有全局 `JOB_DB_LOCK` + SQLite `BEGIN IMMEDIATE` 跨 Graph GET/POST，两次 30 秒超时时可使同进程其他 job SQLite 写阻塞约 60 秒/对象。安全回归已通过，生产性能影响必须在暗发布中观察。

## 验证证据

```text
python -m py_compile app.py scripts/ad_control_rule_runner.py features/ad_control_copy_engine/service.py features/ad_control_execution_log/service.py deploy/apply_ad_control_execution_log_fix.py deploy/apply_ad_control_account_copy_v2.py deploy/migrate_ad_control_account_copy_v2_sqlite.py
结果：退出码 0

node --check static/ad-control-pages.js
结果：退出码 0

Windows 本地从仓库外启动，并设置 `PYTHONPATH=D:\codex\tmp\ad-control-x-stubs;<repo>;D:\codex\tmp\ad-control-test-stubs`；再使用独立 `PYTHONPYCACHEPREFIX` 执行 `python -m unittest discover -s <repo>\tests -p "test_ad_control*.py" -v`。从仓库目录直接启动会让仓库内空的 `features/x_accounts` 先遮住外部 stub，只收集143条并产生3个导入错误，不能作为有效回归结果。
结果：Ran 180 tests / OK / 退出码 0

python -m unittest tests.test_ad_control_account_copy_deploy -v
结果：Ran 12 tests / OK；check 零写、共享锁、source/备份后漂移阻断、持久化唯一备份、mode-before-fsync、安装失败恢复、未知字节不覆盖、二次幂等 / 退出码 0

python -m unittest tests.test_ad_control_account_copy_sqlite_migration -v
结果：Ran 8 tests / OK；dry-run 原库零写、逐ID/product/state常量门禁、首跑3/幂等0、mixed owner/坏pool/TOCTOU失败关闭、触发器副作用事务内回滚、真实 target app ensure/owner 可见性集成 / 退出码 0

python tests/validate_ad_control_deploy_patch.py
结果：当前 merged app 临时副本首次/二次均 app.py unchanged、零备份且字节不变；补丁后全量测试 OK / 退出码 0。需要变更的 current-live 首次 changed+备份路径由下一项验证。

python tests/validate_ad_control_live_action_log_compat.py --live-app <current-live-app.py>
结果：对最终并发 playable 基线 `8c559a78475a7972542746f1f8de1fcab4e7be3f` 重新执行 check、临时 apply、二次幂等和 7 个安全函数验证，全部通过；原 fixture 零写。

git diff --check
结果：退出码 0
```

关键安全断言已覆盖：观察模式 pause/copy 零 Token/Graph 访问；Campaign copy 在 Token/Graph 前失败关闭；mixed 中 copy 提前隔离且 pause 可继续成功，账号维度 mixed 不回查旧 product/account 白名单；Ad execute 顶层短路；stale/损坏 preview 失败关闭，执行前及每次 Meta POST 前重检；enable TOCTOU、同组/全局急停竞态与 V2/legacy save-enabled 绕过均被阻断；ownerless legacy 自动收敛；V2 不误迁移；legacy rule/account-group/rule-set/账户池 owner 隔离；action target 明细 owner 传递；账号规则日志可见和七页 cache buster 一致；空白名单在排期/Token/Graph 前零调用返回；无到期账号零 action且零schema I/O；schema单次失败可重试、持续失败与真实缺列错误码分离；同tick并发只调用一次validator且失败worker异常实例彼此独立；错误总数不随详情截断；旧聚合组迁移失败时完整回滚；未知 action 不会静默转成 pause；部署补丁自包含 audit，对旧基线、泛化 action-log 旧补丁输出和 current-live writer/reader 安全契约均能安全升级且重复应用幂等。

## 遗留风险

- copied created_data/lineage/intent 的 `ads_ai` 分渠道表结构、事务写入和回流扫描尚未设计与实现；本轮不建表、不写入。DDL 环境问题已修复只表示后续获得用户明确授权后可再尝试，因此真实 Meta Campaign copy 必须继续保持关闭。
- 既有 `ads_ai.ad_control_action_log` 是执行审计链路，不是 copied created_data/lineage/intent 或复制结果落表；本次已做代码与 current-live fixture 兼容回归，但未在生产 MySQL 上执行写入验证。
- Ad 第二阶段只有配置契约，没有真实候选扫描、复制、落表或端到端验证。
- Meta copy 编排测试使用 fake/stub；未验证真实 Graph API 版本、账号权限、异步 copy 完成时间、CBO/ABO 实际返回形态和限流行为。
- 原 V2 C2 回滚点位于 `/root/backups/drama_material_service/20260715T103332Z-ad-control-v2-c2-b3c3e6a`；BUG-007 热修 C3 位于 `/root/backups/drama_material_service/20260715T111700Z-ad-control-v2-hotfix-c3-4527303`；BUG-008 schema 热修 C4 位于 `/root/backups/drama_material_service/20260715T120738Z-ad-control-v2-schema-hotfix-c4-375185d`。C2/C3/C4 权限均为 `700`；C3 含 BUG-007 热修前后 SQLite、自然 tick 和健康证据，C4 含 app/runner/静态与配置备份、exact overlay、前后 SQLite、四轮自然 tick、MySQL/HTTP/服务及外部 cron diff 证据。真实复制对象不存在，因此本期无 Meta 补偿项。
- `JOB_DB_LOCK` 跨 Graph 请求的实际负载影响尚无生产证据；上线后须监控 API/runner 耗时、Graph 超时和 job SQLite 写入排队，异常时先停 ad-control runner/禁用受影响 live 组再回滚。
- `datetime.utcnow()` 弃用告警应在后续技术债中改为 timezone-aware UTC。
- legacy standalone `ad_control_rule` 本期按兼容边界保留直接保存/启停，且不受 rule-group 全局急停覆盖。发布前、迁移演练和发布后 total/enabled 均为 0/0，该边界不属于 V2 规则组能力。
- 发布窗口内又发现 18:22 playable preview 并发发布；已停止旧发布流程、恢复服务，将其精确提交 `8c559a78475a7972542746f1f8de1fcab4e7be3f` 合入后重跑全部 staging 与回归，先形成 `b3c3e6a...`，再热修到 `4527303...`。生产 `.env` 哈希保持 `93d8f8ba...`，playable 配置和功能均保留。
- 最终安全审计曾误触 exact deployer 默认 apply，磁盘 app 短暂反向到 `8c559a7`；运行进程未重启，18:49:29 已在下一 cron tick 前按 exact-source 恢复，并重新完成 staging、174/174、热修部署与自然 tick。流程守则已改为只读审计禁止执行 deployer，确需校验必须显式 `--check`，且停止服务前再次读取生产哈希。
- 19:40 BUG-008 由旧版本自然 tick 安全暴露，零 action/Meta 写；19:45 与后续旧版本自然恢复。当前 `375185d` 于 20:11:18 恢复 exact cron，基线 action=17、preview=52、最新对象状态=04:22:58；API/worker/crond active、auth=200、playable unauth=403、public page=200。20:15至20:30四轮自然tick均通过，preview 52→56、action保持17、对象状态不变且部署后日志零schema probe/Traceback。MySQL action_log保持21条，复制熔断变量未设置，延后复制表不存在。
- 验收期间另一个外部任务重新启用了 `tt_minis_multi_dim_dashboard` cron；未覆盖该变更。ad-control runner行仍与C4逐字一致且只有1条，外部diff已留档。

## 发布建议

本期代码已按 GitHub-first、exact-source 窄合并及 C2/C3/C4 回滚点部署，BUG-008生产验收已通过；Campaign 规则配置、observe/preview 和既有 pause 的既有验收边界保持不变。复制总熔断与持久化前置门禁继续关闭；真实 Campaign copy 必须等待 copied created_data/lineage/intent 方案、事务契约和回归测试完成后另行评审，Ad 复制继续作为第二阶段单独验收。
