# 部署文档

## 变更内容

- 规则组模型增加账号归属、对象层级和运行模式。
- runner 支持 Campaign 长期观察、既有 Campaign pause 和 Campaign copy 的生产前置熔断；Ad 规则组本期不能启用。
- Campaign start schema 校验改为关键 MySQL 查询重试，并区分“读取不可用”和“读取成功但真实缺列”；runner 仅在实际需要 campaign-start 查询时按规则组事件做线程安全singleflight，无到期账号不产生 schema I/O。
- 规则组页面移除产品选择，增加复制参数、剧目范围、计划/额度和正式模式确认。
- 本期不新增、不迁移、不写 copied created_data/lineage/intent；DDL 环境问题虽已修复，但只待后续用户明确授权后再尝试。既有 `ads_ai.ad_control_action_log` 是执行审计链路，不是复制结果落表。
- 部署补丁保留当前线上 action-log writer/reader 分离、固定库表、3/5 秒超时、live worker=4 和 runner 更新不立即 upsert 重试的安全契约。

## 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `AD_CONTROL_COPY_ENABLED` | `0` | 复制总熔断；只影响 copy，不影响 pause。本期必须保持 0 |
| `AD_CONTROL_AD_COPY_ENABLED` | `0` | 第二阶段 Ad 复制占位熔断，本期必须保持 0；改为 1 也不能绕过 `phase_not_enabled` |
| `AD_CONTROL_COPY_DAILY_HARD_LIMIT` | `50` | 部署级每日硬上限；正式复制开放后生效 |
| `AD_CONTROL_COPY_USER_DAILY_LIMIT` | `10` | 用户默认每日上限；正式复制开放后生效 |
| `AD_CONTROL_COPY_SOURCE_COOLDOWN_DAYS` | `1` | 来源对象默认冷却期；正式复制开放后生效 |
| `AD_CONTROL_COPY_GRAPH_VERSION` | `v25.0` | 隔离复制适配器版本；本期 app/runner 不调用复制写接口 |
| `AD_CONTROL_COPY_POLL_INTERVAL_SECONDS` | `2` | 隔离编排测试的完成状态轮询间隔 |
| `AD_CONTROL_COPY_POLL_TIMEOUT_SECONDS` | `120` | 隔离编排测试的轮询超时 |

## 数据库边界

- 本次发布不包含 copied created_data、copy lineage、copy intent 的 MySQL DDL、DML 或迁移脚本；DDL 可用性恢复不构成本轮操作授权。
- 不创建或写入 copied created_data/lineage/intent 表。既有 `ads_ai.ad_control_action_log` 仍可按原链路写审计；它不是复制结果落表，不得用它的写入成功替代 copied created_data/lineage/intent 验收。
- SQLite 用于兼容迁移、规则配置、preview/runner 状态和执行日志回退；既有 action log 可继续写 `ads_ai`。
- 正式 Campaign copy 固定在 Meta POST 前返回 `copy_persistence_not_configured`；误开 `AD_CONTROL_COPY_ENABLED` 也不能绕过。

## SQLite owner 迁移前置

- 发布前对线上 SQLite 执行 online backup、`PRAGMA integrity_check` 和文件 hash；在备份副本上先跑 schema ensure，再比较规则组总数、enabled 数和 owner 分布。
- 精确列出 `owner_user_id=''` 的行及其 `created_by`。有可验证 `created_by` 的行可按原值回填；owner/created_by 双空行由迁移自动收敛为 `enabled=0, emergency_stopped=1`。
- 若历史 `created_by` 是服务账号（例如 `codex`）而实际归属是当前优化师，不得批量猜测；核对 session 用户、历史操作记录和备份后，只对明确的 group ID 执行事务内 owner 更新，保留 `created_by`。
- 迁移后重复运行 ensure，确认 `product=''` 的 V2 组未被创建 `legacy_*` rule set，旧 live pause 行为未改变。
- 只使用审核提交内的 `deploy/migrate_ad_control_account_copy_v2_sqlite.py`。默认命令在 SQLite 临时 backup 上完成完整 ensure+owner 演练且不改原库；正式迁移必须追加 `--apply`。`--app` 必须指向已核对 target hash 的 staging/生产 `app.py`，不能误用旧 checkout。

```bash
python3 deploy/migrate_ad_control_account_copy_v2_sqlite.py \
  --db /root/drama_material_service/data/drama_material_jobs.sqlite3 \
  --app /root/drama_material_service/app.py \
  --owner 892fd2e8 --expected-created-by codex \
  --expected-group-state frg_plus8_non_asian_lang_10am_dramawave_binding:dramawave:1:0:0 \
  --expected-group-state frg_plus8_non_asian_lang_10am_freereels_binding:freereels:0:0:1 \
  --expected-group-state frg_plus8_non_asian_lang_10am_hotdrama_binding:hotdrama:0:0:1

# 仅在全部 writer 已停止、最终 C1 完成且上述 check 通过后执行同命令并追加：
# --apply
```

## Current-live action-log 兼容门禁

- 每次部署前重新下载当前线上 `app.py`，校验其 SHA-256 与本地 current-live fixture 一致；如线上哈希已变，必须刷新 fixture 并重跑验证，不得沿用旧结论。
- 执行 `python tests/validate_ad_control_live_action_log_compat.py --live-app <current-live-app.py>`。验证器只读原 fixture，仅在临时副本上 apply。
- 必须同时满足：首次写前备份与 fixture 字节一致；二次 check/apply 均 `unchanged`；writer 使用 63353、reader 使用 63350；connect/io timeout 为 3/5 秒；`AD_CONTROL_LIVE_MAX_WORKERS=4`；runner 状态更新无立即 upsert 重试；7 个既有线上 action-log 安全函数 hash 不变。
- 任一契约失败、fixture 发生未解释漂移或补丁无法幂等时，本次发布立即停止。

## Legacy standalone 发布边界

- `ad_control_rule` 是早于规则组 V2 的 standalone 规则表。本期不改它的保存/启停入口，rule-group 全局急停也不覆盖该表；这是兼容既有关闭规则的 grandfather 边界，不得误报为 V2 急停已覆盖全部旧规则。
- 发布前在 SQLite 只读执行 `SELECT COUNT(*) AS total_count, COALESCE(SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END),0) AS enabled_count FROM ad_control_rule;`，并单独导出 enabled 行的 `rule_id,name,product,action,created_by,criteria_json,schedule_json,thresholds_json` 或为这些稳定字段生成 row hash。不得查询或输出任何 Token/secret。
- 将查询结果与 C1 备份基线一起保存；overlay/迁移演练和发布后复查必须保持 standalone 总数、enabled 集合及上述稳定字段/row hash 不变。若存在未预期 enabled 行、归属不明或前后集合漂移，停止部署并由业务确认。
- 在备份副本完成 additive schema ensure 后，导出 `ad_control_rule_group -> ad_control_account_group` 关联的 `group_id/account_group_id/product/owner_user_id/created_by`。账户池缺失、非空 product 不一致、以及不满足“同 owner 或双方 `created_by` 相同”兼容谓词的非法跨 owner 数量必须为 0；合规的既有服务 link 需逐条列出。任何未解释引用均停止发布。

## 共享发布锁与排他窗口

- P0 前置：所有受控的生产 `app.py` 写入者必须使用同一个 `/var/lock/drama-material-service.deploy.lock`。本次发布窗口禁止其他部署、热修和人工覆盖；advisory lock 不能阻止绕锁的 `root/cp`，因此仍必须依赖排他窗口及发布前后 SHA-256 门禁。
- `deploy/apply_ad_control_execution_log_fix.py` 只能对临时 current-live 副本做兼容验证，禁止以 write 模式直接修改生产 `app.py`。
- 生产 `app.py` 只能由 `deploy/apply_ad_control_account_copy_v2.py --lock-file /var/lock/drama-material-service.deploy.lock` 安装。锁内完成最终 source 重读、持久化唯一备份、原子替换和失败恢复；已有备份复用前也必须重新校验并 `fsync` 文件与目录。
- C1 备份目录必须在发布前创建并持久化；脚本还会对 backup root 及父目录执行目录 `fsync`。任何锁冲突、source/target/hash/备份不一致或恢复失败都立即停止发布。

```bash
# 服务仍运行时：只验证 staging 副本，STAGING_ROOT 不能指向生产目录。
python3 deploy/apply_ad_control_account_copy_v2.py --root "$STAGING_ROOT" --repo "$RELEASE_REPO" --source-commit "$SOURCE_COMMIT" --target-commit "$TARGET_COMMIT" --backup-dir "$RELEASE_ROOT/staging-backup" --lock-file "$RELEASE_ROOT/staging.deploy.lock"

# 仅在 runner/API 已停止且 C1 完成后：生产 root 唯一一次安装。
python3 deploy/apply_ad_control_account_copy_v2.py --root /root/drama_material_service --repo "$RELEASE_REPO" --source-commit "$SOURCE_COMMIT" --target-commit "$TARGET_COMMIT" --backup-dir "$C1_DIR/app-exact-backup" --lock-file /var/lock/drama-material-service.deploy.lock
```

## 部署步骤

1. 本地完成 Python/JavaScript 语法检查、180/180 全量单测、exact-source 发布器 12/12、SQLite owner 迁移器 8/8、临时 SQLite 和 Stub Meta 隔离测试。
2. 刷新 current-live `app.py` 和 SHA-256，通过上述 action-log 兼容门禁；这一步必须在最终发布文件冻结前重跑。
3. 精确 commit/push 到 GitHub，记录 commit 和文件 hash。
4. 建立排他发布窗口并确认其他生产 `app.py` 写入者均停用或使用统一锁。备份线上 `app.py`、runner、静态文件、cron、systemd、Nginx、`.env` 和 SQLite，执行 SQLite online backup 与 integrity check；保存 rule-group owner 基线及 legacy standalone total/enabled 集合。
5. 从当前线上共享 monolith 的只读副本建立 staging root；此步只能对 staging root 运行 exact 发布器的 check/apply，并核对 staging `app.py` 等于 target blob，绝不写生产 root。禁止用仓库整份 `app.py` 盲目覆盖线上文件。
6. 在 C1 SQLite 备份副本上先演练 schema ensure 和仅针对已核实 group ID 的精确 owner 迁移；只有行数、owner、enabled/急停和 V2 幂等校验一致时才能继续。
7. 先用 `systemctl`、cron 清单、`lsof/fuser` 和代码路径枚举所有会打开或写入精确文件 `data/drama_material_jobs.sqlite3` 的进程；安全收敛在途任务后，暂停 ad-control runner cron，并停止 API、`drama-material-job-worker.service` 及任何其他同库 writer。确认无 runner、worker 或同库文件占用后，才建立“停止写入”的最终 SQLite C1。若不能静默全部 writer，本次迁移和上线立即停止，禁止使用可能覆盖新任务状态的整库恢复方案。随后保持两个复制熔断为 0，在排他窗口内只对生产 root 唯一一次运行 `apply_ad_control_account_copy_v2.py --lock-file /var/lock/drama-material-service.deploy.lock`，再原子替换其余审核文件；此时不得启动任何同库服务。
8. 在全部 writer 已停止并完成最终 C1 后，用 systemd 相同的 `/usr/bin/python3` 和生产依赖对真实 DB 再运行一次 owner 迁移器默认 check；在线旧 check 不能替代此步。check 通过后立即以完全相同参数追加 `--apply`，在同一停止窗口内完成 additive ensure 和三条精确 owner 更新，保留 `created_by='codex'`；断言脚本内锁死的 group ID/product/enabled/急停/deleted、首跑更新3行或幂等0行、账户池/解析账号、rule set、legacy standalone 0/0、事务前后完整非owner快照和二次 ensure 幂等。任何失败时全部同库服务保持停止，并按回滚边界判断是否恢复 C1。
9. 仅在真实 SQLite 迁移全部断言通过后执行语法检查并启动 API；完成 API/页面 smoke 后恢复 `drama-material-job-worker.service` 并确认任务状态/心跳正常，最后恢复 ad-control runner cron并观察至少一个自然 tick。
10. 执行规则组 smoke test，确认新组默认 disabled/observe，save 携带 `enabled=true` 仍无法开启，既有 pause 可试算/执行，copy 正式执行返回 `copy_persistence_not_configured`。
11. 只开放已批准账号的 Campaign observe 模式，运行至少一个账号自然日并审计 would_* 结果；Ad 规则组保持 disabled。
12. 本期到此结束；不得进行 PAUSED/ACTIVE 复制 Canary。持久化方案确认后另走完整评审与发布流程。

## 验证步骤

- `systemctl is-active drama-material-api.service` 为 `active`。
- `/api/health`、规则组列表和规则组页面返回正常。
- 新建规则默认 disabled/observe；切 live 或正式启用缺少 `ENABLE_LIVE_MODE` 时被拒绝。
- 普通 save payload 即使含 `enabled=true` 也不能开启规则组；损坏 preview `expires_at` 返回 `preview_invalid`。
- Token 校验期间触发同组或当前用户全局急停时，启用返回 `emergency_stop_changed` 且保持禁用；若请求开始前已急停且期间无新急停，只有完成 preview/Token 校验的显式恢复可清除旧急停。
- Campaign observe runner 日志有 `would_pause/would_copy`，Meta 写调用为 0；Ad 启用和试算返回 `phase_not_enabled`。
- `no_accounts_due` tick 对 campaign-start schema 探测为 0；实际需要读取 schema 时使用 critical retry，读取持续失败返回 `insight_start_schema_unavailable`，读取成功但缺 `campaign_id/dt` 才返回 `invalid_insight_start_schema`。
- 无新增复制结果表、无 copied created_data/lineage/intent DDL/DML；既有 `ads_ai.ad_control_action_log` 审计链路保持不变，且不将该审计表计为复制结果落表。
- 最终部署前刷新后的 current-live fixture 通过 action-log 兼容验证，writer/reader、3/5 秒超时、live worker=4、无立即 upsert 重试和 7 个函数 hash 契约均不回退。
- 即使测试进程中临时设置 `AD_CONTROL_COPY_ENABLED=1`，正式 copy 仍返回 `copy_persistence_not_configured` 且 Meta POST 为 0。
- copy 熔断不影响既有 Campaign pause 回归。
- ownerless legacy 组为 disabled+急停，已核实 owner 的历史组在当前用户下可见；重复 ensure 不改变账号维度 V2 组。
- 规则组不能引用其他 owner 的账户池；日志列表/目标明细只能读取本人记录，默认“全部产品（含账号规则）”能显示 `product=''` 的 V2 日志；所有 ad-control HTML 的 CSS/JS cache buster 一致。
- 账户池关联审计中缺失引用、非空 product 不一致及不满足兼容谓词的非法跨 owner 均为 0；若存在同 `created_by` 的合规服务 link，其 group/pool ID 与 C1 清单一致。
- legacy standalone `ad_control_rule` 的总数、enabled 集合和业务字段与 C1 基线一致，且明确记录其不受 rule-group 全局急停覆盖。
- 监控正式 pause 的 Graph GET/POST 耗时、ad-control runner tick 耗时、同进程 job SQLite 写入排队/超时。当前 P2 上界是单 Campaign 两次 Graph 30 秒超时导致约 60 秒写阻塞；生产实际基线只能由暗发布观察证明。

## 回滚方案

- 复制相关异常：保持 `AD_CONTROL_COPY_ENABLED=0`；本期没有任何 Meta copy 对象或 copied created_data/lineage/intent 数据需要补偿。既有 action log 审计可能存在，应保留而不是回滚删除。
- 应用异常：默认只恢复最近检查点的 app、runner、静态文件和 cron/systemd/Nginx，重新语法检查后原子替换；不得因代码或页面异常顺带覆盖共享 SQLite。
- live SQLite schema/owner 迁移任一断言失败：API、worker、runner 及全部同库 writer 保持停止，恢复停止写入后的 C1 SQLite 与对应文件检查点，复跑 integrity/owner/standalone/任务状态基线后，按 API→worker→ad-control cron 顺序恢复旧服务。
- 静态页面异常可单独恢复上一检查点的 HTML/CSS/JS，不改后端和 SQLite。
- runner 异常先停对应 cron，再只恢复 runner 文件；既有 pause 回归通过后再启 cron，不回滚共享 SQLite。
- 若 `JOB_DB_LOCK` 跨 Graph 请求导致 API/runner 耗时或 job 写入排队超出可接受基线，先停 ad-control runner并禁用受影响 live 规则组，不删审计数据；然后恢复上一检查点 app/runner。只有明确必须撤销 owner/schema、再次静默全部同库 writer，并已比较 C1 后任务/lease/event 增量确认不会丢数据时，才允许按上一条恢复整库；否则保留已迁 SQLite，仅回滚代码。
- 所有回滚均记录目标 commit、文件 hash、备份目录和 SQLite integrity check。

## 注意事项

- 生产是共享 monolith，必须从线上现行副本窄合并并逐文件核对，不能覆盖未进仓库的线上改动。
- 不在本期运行任何 Meta copy Canary，不尝试寻找或使用 copied created_data/lineage/intent 的 ads_ai 写端点；既有 `ad_control_action_log` 链路保持运行。
- 用户已确认 DDL 环境问题修复，但本轮没有复制结果建表/写入授权；不得因环境可用而临时扩大部署范围。
- 后续接入持久化时，Campaign copy 和 Ad copy 分别重复设计评审、观察、PAUSED Canary 和 ACTIVE 放量流程。

## 当前发布状态

2026-07-15 已完成原 V2、BUG-007/BUG-008 热修、服务/API smoke及最终四轮自然 runner 复验，发布验收闭环。发布窗口内发现 18:22 并发上线的 playable preview hardening 后，立即停止原发布、恢复服务，并以其精确提交 `8c559a78475a7972542746f1f8de1fcab4e7be3f` 和线上 `app.py` SHA-256 `8779e156...` 作为新 source。合并并保留 playable generator、vendor、Nginx 和 9 个 `PLAYABLE_PREVIEW_*` 环境配置后，原 V2 运行提交为 `b3c3e6a2d6556d7dad4c79082a324235ad0f8379`。

原 V2 staging `/root/releases/ad-control-v2-20260715-183100-b3c3e6a` 和 C2 回滚点 `/root/backups/drama_material_service/20260715T103332Z-ad-control-v2-c2-b3c3e6a` 均完整保留。首次恢复 cron 的 18:50 自然 tick 暴露 BUG-007：空 Campaign 白名单被误计为 108 个 preview 错误，runner 安全阻断为 `live_preview_blocked`；requested/success 均为 0，未读取 Token、未调用 Graph、未发生 Meta 写入。下一 tick 前仅暂停 ad-control cron，API/worker 未受影响。

BUG-007 热修提交为 `7f65cf9bf6799fb0a086238d41f569c2b206e820` 和 `4527303100a38db26f0f2ac0825ed6616c16247a`，staging 为 `/root/releases/ad-control-v2-hotfix-20260715-190000-7f65cf9`，该阶段生产 Python 环境 fresh-cache 174/174 通过。该阶段 `app.py` SHA-256 为 `39b81d10cab7bf28a60132fb5445d0a2bedbc817bad3160dc6b39d495667764e`，runner SHA-256 为 `2e285a46f5054f7e8dbfa7ae66bbe7145ca9d28a701cfdd94efe46f3ee990347`。热修 C3 回滚点为 `/root/backups/drama_material_service/20260715T111700Z-ad-control-v2-hotfix-c3-4527303`，C2/C3 权限均为 `700`。

生产 SQLite 迁移默认 check 零写，首次 apply 精确更新 3 个已核实规则组 owner，二次 apply 更新 0 行；`integrity_check=ok`，当前用户 owner 为 `892fd2e8`，active Dramawave 规则组解析 245 个账号，legacy standalone total/enabled 保持 0/0，未创建 copy intent/lineage/quota 表。API、worker、Nginx、认证页面、playable 未授权 403、真实浏览器规则组页面和静态版本 `20260715copylog3` 均通过；浏览器未保存任何编辑。

原 crontab 已与 C2 备份逐字节恢复，SHA-256 为 `9f89d934...`，唯一 runner 条目为 1。19:25 自然 tick 返回 `skipped/no_accounts_due`，requested/success/error 均为 0、action_id 为空；SQLite action 数量前后保持 17，最新 preview 的 `scheduled_due_count=0`、`preview_error_count=0`、candidate/pause/copy target 均为 0。API/worker/crond 均 active，应用日志无 Traceback/SyntaxError/ImportError。

连续监控在 19:40 发现 BUG-008：旧代码执行 `SHOW COLUMNS FROM kunlunads_dev.ads_facebook_hours_insights` 时遇到一次 MySQL client exit 1，best-effort helper 将读取失败返回为空集合，进而误报 `insight start table missing required fields`。该 tick 没有 preview_id/action_id，requested/success/error 均为 0，action 保持 17、对象状态未变化，且未调用 Meta 写接口。19:41 人工只读回查确认表有 54 列且 `campaign_id/dt/ad_account_id` 均存在；19:45 及后续旧版本自然 tick 自行恢复，证明不是 schema 漂移。

BUG-008 修复提交和当前生产运行版本为 `375185d5c7ad8dbdf39eae8e5c8b8ddf7a45b9a5`，staging 为 `/root/releases/ad-control-v2-schema-hotfix-20260715-195900-375185d`，生产 Python 环境 fresh-cache 180/180 通过。当前生产 `app.py` SHA-256 为 `7ed60179abc83880d41f2547ed19e3591136dca693c776df5d5ecfe6a2546b49`，runner SHA-256 为 `a3fa7b2bbe597e52dec44347de750fe34d313302baefba585f66d521dd5c25e7`；C4 回滚点为 `/root/backups/drama_material_service/20260715T120738Z-ad-control-v2-schema-hotfix-c4-375185d`。修复将 schema 读取纳入 critical retry，并分离持续读取失败与真实缺列语义；runner 在每次规则组事件内lazy singleflight，失败时为各 worker 构造独立异常，无到期账号不触发 schema I/O。

20:11:18 已恢复 exact ad-control cron。恢复前基线为 SQLite action 17、preview 52，最新对象状态时间仍为 04:22:58；`drama-material-api.service`、`drama-material-job-worker.service`、crond 均为 active，认证接口 200、playable 未授权 403、公开页面 200。20:15、20:20、20:25、20:30 四轮自然 tick 均返回 `skipped/no_accounts_due`，requested/success/error 均为0；preview 52→56，action保持17，对象状态保持不变，六项preview指标均为0，部署后日志无schema probe、`SHOW COLUMNS`或`Traceback`。MySQL只读回读为`read_only=1`、既有action_log保持21条，复制熔断变量仍未设置，copied created_data/lineage/intent表仍不存在。

20:11恢复后，另一外部任务将 `tt_minis_multi_dim_dashboard` cron 从事故暂停改为启用，导致root crontab整体SHA从C4基线变化；该差异不属于本发布且未被覆盖。ad-control runner行仍与C4逐字一致且仅1条，最终审计保留了该外部diff和当前SHA。

最终安全审计期间曾有一次只读审计误触 exact deployer 的默认 apply，将磁盘 `app.py` 从 `b3c3e6a` 反向应用到 `8c559a7`；运行中的 API 进程未重启，检测到哈希漂移后于 18:49:29 使用相同 exact-source 链恢复到 `b3c3e6a`，早于 18:50 cron tick，未有请求在错误磁盘版本上执行。后续 staging、174/174、热修 overlay 和自然 tick 均基于重新读取的精确哈希完成。只读审计今后禁止运行 deployer；如确需验证只能显式使用 `--check`。

本次只上线配置、观察/试算、既有 pause 兼容和复制前置熔断。`AD_CONTROL_COPY_ENABLED`、`AD_CONTROL_AD_COPY_ENABLED` 未配置/保持关闭；正式 Campaign copy 在 Token/Graph 前失败关闭，Ad 执行仍返回 `phase_not_enabled`，没有 Meta copy、没有 copied created_data/lineage/intent DDL/DML，也没有复制 Canary。
