# 已恢复队列的 Auto 回执跟踪

问题：原生队列通过本次逐条审计恢复后，Auto 的 terminal `failed` 记录不会被普通轮询领取。原生后来成功，Auto 仍显示旧失败。已确认成功的记录用既有 `reconcile_x_auto_receipts.py` 校正；本入口仅为尚在排队/制作/发布的队列恢复只读回执跟踪。

`scripts/rearm_x_auto_receipt_tracking.py` 只修改 Auto SQLite，不调用 X、publisher、恢复 API 或选材器；原生和中央数据库通过只读连接查询。普通 failed 自动重试策略不变。

## 精确准入

- 必须显式指定 Auto task ID，所属上海日期为 2026-11-02 或 2026-11-03；状态为 failed、unknown_outcome=0、无 Post ID、无 claim/worker/lease。取消的父 run 拒绝。
- 必须匹配本次现场核验的 5 个 prepost plan hash（79 队列）之一，且中央 journal committed 与原生逐代授权 proof 完全一致；或者匹配本次 2 个归因恢复 plan hash（7 队列）的原子中央审计事件。其他恢复记录、未完成 staged 和未审计失败拒绝。
- Auto execution_run/queue、原生唯一 manual parent/queue/log、账号、素材、content、模板版本、正文模板、材料预留完全一致。原 Auto execution_log_id 为空时独立绑定唯一原生日志但保持该字段为空；非空时必须与原日志 ID 相同。
- 当前原生队列仍在正常处理，日志无 unknown/Post 成功，中央无 unknown/hold，原生和 Auto 无 canonical hold。已成功记录在 plan 中跳过；plan 后新成功也跳过并提示使用成功回执对账入口。新的未知或身份漂移使 apply 的 CAS 拒绝。
- q5280 不在允许集合内，新的初始化 403 失败不重新放行。其他 unknown 拒绝；仅下面已完整独立对账的 Auto 2395 有保留 unknown 标记的精确观察分支。

### Auto 2395 / q5524 的独立观察授权

原生 q5524 的完整独立对账已由主流程提交，计划指纹固定为 `66167b334a1bb019987a4761a6d022bbf2f5f98b673b70f78262313797e02585`。Auto 2395/run414/parent1563 仍是 failed/unknown1，所以普通 unknown0 分支不会处理它。此精确分支同时要求：

- Auto 完整原始行指纹与本次只读快照相同；不能借用该身份恢复后来的新错误。
- 两库 q5524 append-only 审计保存相同完整原对账 plan，内容指纹与固定已提交计划一致；中央 journal 必须 committed。
- 原生仅有该 source 的唯一 1→2、prior_attempts=1 授权，且授权 proof 与原完整对账一致；当前唯一 queue/log/parent、冻结正文和素材身份不变，原生没有新的 unknown 或成功记录。

它仍只将 Auto 切换到 retry_wait/reconcile，**保留 Auto unknown_outcome=1**、原错误、全部 execution IDs 与历史。随后实际 dispatch runtime 只观察当前原队列；普通排队/上传观察不会清除该 unknown，唯一真实成功回执才通过既有 publisher 逻辑清除。新 native unknown、审计缺失/未提交/漂移或 Auto 行变化一律拒绝。没有新的平台发布授权，也没有对其他 unknown 开放例外。

## 实际 runtime 校验

plan 与 apply 都直接读取 `x-auto-post-service.service` MainPID，以及该 PID 的 `/proc` 环境、启动时间、命令行、cwd 和 5 个 owning 源文件哈希。必须实际运行时 `PIPELINE_X_DISPATCH_ENABLED=1`、canonical bridge=1，配置的 Auto DB 路径与目标一致。没有可用 CLI 布尔值跳过此检查。读取秘密仅发生在内存中，输出只包含允许的控制状态。

已审查 publisher 的 dispatch 分支：`retry_wait + claim_phase=reconcile` 被领取后只执行 `_reconcile`，使用原 execution_run/queue 查询原生状态；队列活跃则继续观察，终态则从原回执更新 Auto，不触发 `publish_queue`、`recover_run` 或重新选材。运行 service 中独立的账号批量校验改动也已只读审查，不涉及 executor 的 dispatch 注入；该变更原样保留。

Runtime 在 plan 前后、apply 前以及 Auto 事务 CAS 完成前复核。PID 重启、开关、数据库路径、未审查代码变化都拒绝当前计划，应重新只读检查，不放松规则。

## 原子更新与幂等

先以私有权限备份 Auto 数据库并做 quick_check。单个 Auto `BEGIN IMMEDIATE` 事务核对整个选中集合，再将每条任务改为 retry_wait/reconcile，设置下次观察时间并清除旧终止时间；execution IDs、发布/制作次数、原错误保持。材料预留的状态同步为 retry_wait。必要的 terminal 父 run 恢复 running，原错误保持，原完整状态保存在审计与事件中。

`x_auto_receipt_tracking_resume_audit` 保存完整原任务、父 run、材料预留、原生身份和状态快照、本次恢复授权、runtime 证明、备份路径及 actor；禁止 UPDATE/DELETE/INSERT OR REPLACE 覆盖。审计、任务、材料预留、父 run 与事件同一事务提交。任何一步失败全部回滚。

同一计划重复执行只检查已有审计并 no-op，不把后续 published、unknown 或新 failed 再次变为 retry_wait。计划期间原生上传的状态/attempt 正常推进不要求停 worker；冻结身份和恢复授权仍须一致。

```bash
python3 scripts/rearm_x_auto_receipt_tracking.py plan \
  --auto "$AUTO_DB" --native "$NATIVE_DB" --central "$CENTRAL_DB" \
  --task-ids $EXACT_TASK_IDS --output "$PRIVATE_DIR/auto-tracking-plan.json"

# 复核计划中的 items / skipped，再按本次用户授权执行：
python3 scripts/rearm_x_auto_receipt_tracking.py apply \
  --plan "$PRIVATE_DIR/auto-tracking-plan.json" --backup-dir "$PRIVATE_DIR/auto-tracking-backups" \
  --actor 'codex:user-authorized-20261103' --output "$PRIVATE_DIR/auto-tracking-applied.json"
```

输出文件必须尚不存在且留在私有目录。父 run 恢复 running 只是使显示与观察进度一致；只有之后原生真实成功回执才将任务标记 published。

## 本地验证

专项覆盖真实 Auto store 领取 reconcile、原队列 queued→published 的只读跟踪且发布/恢复/选材调用为零、原执行 ID/次数/错误保留、同父多任务单事务、append-only 与幂等、原生上传继续推进、未知/未授权/活跃 claim/取消拒绝、运行开关或代码漂移、身份漂移、已成功跳过及中途回滚。

```bash
python -m unittest scripts.test_x_auto_receipt_tracking scripts.test_x_auto_receipt_reconciliation scripts.test_x_auto_post_publisher scripts.test_x_auto_post_store scripts.test_pipeline_x_planning -q
```

实现阶段未执行生产 apply，也未更改 runtime 或启停服务。生产计划、备份、实际恢复数量及最终回执由主部署流程验收。

2026-11-03 组合 73 项通过，其中专项 12 项；追加原生冻结 queue/body 授权哈希校验后专项再次通过，编译通过。

后续精确 2395 分支新增 4 项测试，验证保留 unknown 的 queued→published 观察且 publish/recover/record 调用为零、两库审计或 committed 缺失、原 Auto/新 native unknown 漂移、计划指纹替换拒绝。

补充分支后的组合 77 项全部通过（专项 16 项），编译通过。
