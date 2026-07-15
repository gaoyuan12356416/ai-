# 部署文档

## 变更内容

- 规则组模型增加账号归属、对象层级和运行模式。
- runner 支持 Campaign 长期观察、既有 Campaign pause 和 Campaign copy 的生产前置熔断；Ad 规则组本期不能启用。
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

## 部署步骤

1. 本地完成 Python/JavaScript 语法检查、107/107 全量单测、临时 SQLite 和 Stub Meta 隔离测试。
2. 刷新 current-live `app.py` 和 SHA-256，通过上述 action-log 兼容门禁；这一步必须在最终发布文件冻结前重跑。
3. 精确 commit/push 到 GitHub，记录 commit 和文件 hash。
4. 备份线上 `app.py`、runner、静态文件、cron、systemd、Nginx、`.env` 和 SQLite，执行 SQLite online backup 与 integrity check；保存 rule-group owner 基线及 legacy standalone total/enabled 集合。
5. 从当前线上共享 monolith 副本做窄合并；禁止用仓库整份 `app.py` 覆盖线上文件。
6. 在备份 SQLite 副本上先演练 schema ensure 和精确 owner 迁移；只有行数、owner、enabled/急停和 V2 幂等校验一致时才能继续。
7. 保持两个复制熔断为 0，原子替换代码和静态资源；使用已验证的补丁链完成写前备份、应用和幂等检查，语法检查通过后窄重启所需服务。
8. 执行页面、API、runner smoke test，确认新组默认 disabled/observe，save 携带 `enabled=true` 仍无法开启，既有 pause 可试算/执行，copy 正式执行返回 `copy_persistence_not_configured`。
9. 只开放已批准账号的 Campaign observe 模式，运行至少一个账号自然日并审计 would_* 结果；Ad 规则组保持 disabled。
10. 本期到此结束；不得进行 PAUSED/ACTIVE 复制 Canary。持久化方案确认后另走完整评审与发布流程。

## 验证步骤

- `systemctl is-active drama-material-api.service` 为 `active`。
- `/api/health`、规则组列表和规则组页面返回正常。
- 新建规则默认 disabled/observe；切 live 或正式启用缺少 `ENABLE_LIVE_MODE` 时被拒绝。
- 普通 save payload 即使含 `enabled=true` 也不能开启规则组；损坏 preview `expires_at` 返回 `preview_invalid`。
- Token 校验期间触发同组或当前用户全局急停时，启用返回 `emergency_stop_changed` 且保持禁用；若请求开始前已急停且期间无新急停，只有完成 preview/Token 校验的显式恢复可清除旧急停。
- Campaign observe runner 日志有 `would_pause/would_copy`，Meta 写调用为 0；Ad 启用和试算返回 `phase_not_enabled`。
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
- 应用异常：恢复最近检查点的 app、runner、静态文件、cron/systemd/Nginx 和 SQLite，重新语法检查后原子替换。
- 静态页面异常可单独恢复上一检查点的 HTML/CSS/JS，不改后端和 SQLite。
- runner 异常先停对应 cron，再恢复 runner 和 SQLite online backup；既有 pause 恢复后再启 cron。
- 若 `JOB_DB_LOCK` 跨 Graph 请求导致 API/runner 耗时或 job 写入排队超出可接受基线，先停 ad-control runner 并禁用受影响 live 规则组，不删审计数据；然后恢复上一检查点 app/runner。只有 owner/schema 迁移也需撤销时才恢复对应 SQLite online backup。
- 所有回滚均记录目标 commit、文件 hash、备份目录和 SQLite integrity check。

## 注意事项

- 生产是共享 monolith，必须从线上现行副本窄合并并逐文件核对，不能覆盖未进仓库的线上改动。
- 不在本期运行任何 Meta copy Canary，不尝试寻找或使用 copied created_data/lineage/intent 的 ads_ai 写端点；既有 `ad_control_action_log` 链路保持运行。
- 用户已确认 DDL 环境问题修复，但本轮没有复制结果建表/写入授权；不得因环境可用而临时扩大部署范围。
- 后续接入持久化时，Campaign copy 和 Ad copy 分别重复设计评审、观察、PAUSED Canary 和 ACTIVE 放量流程。

## 当前发布状态

2026-07-15 已完成本地 fresh-cache 107/107 回归。`python tests/validate_ad_control_deploy_patch.py` 验证当前 merged app 临时副本首次/二次 apply 均为 `unchanged`、零备份、字节不变且全量通过；验证器对需要变更的输入仍强制唯一写前备份。`validate_ad_control_live_action_log_compat.py` 已对当前线上快照证明首次 changed、写前备份字节一致、二次幂等和 writer/reader 安全契约不回退。

服务器异常重启后已再次只读核验：`drama-material-api.service`、crond 和 5 分钟 runner 均正常；SQLite `integrity_check/quick_check=ok`；legacy standalone `ad_control_rule` total/enabled 均为 0；V2 标记、V2 schema 列和 copy intent/lineage/quota 表均不存在。线上 app/runner/action-log service/migration/CSS/JS/rules HTML 仍与安全生产快照 `146cb1b50b60ee3fe4c53de6d6d40d980124483c` 字节一致，确认中断前没有开始 V2 部署。仅存在 C0 备份，C1 尚未建立。

只读核验同时发现无关文件 `doc/deployment/playable-preview-api.md` 有并发修改。正式部署前必须重新抓取 live 全量基线并保留该变更，只覆盖本需求精确文件。生产 overlay 在生产 Python/依赖环境的完整测试、GitHub exact-commit 发布、C1 备份、服务重启、暗发布与线上 smoke test 均尚未完成；本文档不得作为“已上线”证据。
