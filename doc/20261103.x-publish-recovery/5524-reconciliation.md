# q5524 精确历史对账与下一代授权

范围仅为队列 5524、日志 5475、prepare job 31955、publish job 31993、账号 43、auto_template 父任务 1563 的第 1 代。用户授权恢复昨日和今日未完成任务，同时要求结果不明不得重发。本入口先将这一代独立核实为未进入 Post，再使用既有的逐代授权；不改变全局 unknown 守卫，不处理其他 unknown。

## 历史结论依据

- HK GPU 原 worker `hk-gpu-publish-0-ff381fa0`、PID 198472，与 `gpu-offload-20261103T090605Z/audit.json` 的 `children_before` 和 `previous_sha` 对上；原 unit 和 release 文件有独立内容指纹。对应完整 Git SHA 为 `ec8984773c4bc33e832ec5ae483cb3dcad7482dc`，不是用今日新部署版本推测历史行为。
- 原版本 `worker.py` 只有在同步 executor 返回或抛错后才写终态失败 outbox。原 ACK `b52ca6ec1895491f972ee928b9b952d5` 保持 `unknown_outcome=true`、`pipeline_lease_lost` 和原上传阶段指标，中央 delivery 的原请求指纹、claim 指纹及 accepted/unknown 响应一致。
- 原版本 `executors.py` 的真实 X 请求先经过 `context.operation` 取得许可和凭据。上传完成并记录 `x_media_uploaded` 后才调用 create_post。该队列中央和原生各 83 条许可逐条一致，仅 initialize/append；没有 finalize、Post、Repost 或后续 checkpoint。原日志没有媒体 ID、Post ID 或 published_at。
- 当前节点证据要求 CPU 没有本任务回执，HK GPU 恰好保留原 4 个 ACK 包：prepare preflight、prepare complete、publish media_initialized、publish fail。每包同时绑定原始 JSON 指纹与移除 claim token 后的 JSON 指纹；任何新增、pending、内容变化或节点清单变化均拒绝。所有原始证据完整留入私有审计，claim token 只保留哈希。

这组证据共同证明原执行在媒体上传阶段终止。单独的“没有 Post intent”不是本入口的授权依据；其他任务不能套用此结论。

## 实现与中断边界

`scripts/reconcile_x_5524_prepost.py` 无可变 ID 参数，不调用 X。只读 plan 保留完整 queue、原 unknown 日志、旧 attempt=1、generation、冻结正文/链接/账号/成片、父任务、两库操作与 claim 历史、全部 checkpoint、delivery、receipt、event 以及账号隔离记录。精确历史指纹和 CAS 排除新增操作、Post、活动租约或冻结身份变化。

apply 在私有目录备份两库并校验备份，之后有三个持久阶段：

1. 中央仍为 unknown，并添加专用 hold；中央写入 append-only 原证据与 staged journal，账号 43 隔离保留。
2. 在中央写锁下复核两库，原生写入 append-only 对账记录和既有 `pipeline_x_prepost_recovery` 的唯一 1→2 授权；原日志变为 reserved、unknown 标志清零，attempt 仍为 1，历史错误和 started_at 保留。父任务按原队列聚合恢复 running。原生旧 claim 被现有 runtime 守卫拒绝。
3. 再次复核两库及未消费授权，在单个中央事务中恢复该 job 为 ready、释放仅该 job 的账号隔离并标记 committed。旧操作、历史 claim、delivery、receipt 与失败事件不删除。

两库不是一个分布式事务。前两个阶段任意中断时，中央 unknown/hold/隔离阻止执行；用同一 plan 和重新采集的两节点证据续跑。每阶段都检查证据在 120 秒内，备份耗时导致过期也不放行。审计禁止 UPDATE/DELETE；进度 journal 只记录 staged/committed。完成后重复 apply 直接 no-op，即使下一代已消费授权，也不重置新尝试。

下次正常 claim 才递增 generation 至 2，首次 initialize 才原子消费授权并将原日志 attempt 从 1 增至 2。原素材、冻结正文、账号语言、权限和账号占用校验全部继续生效。禁止清理审计来重做计划，禁止用全库备份覆盖仍在工作的生产数据库。

## 运维接口

所有路径取当前部署配置。计划和节点证据留私有目录，不提交 Git。部署主流程推送并检出本提交后执行：

```bash
python3 scripts/reconcile_x_5524_prepost.py plan \
  --central "$CENTRAL_DB" --native "$NATIVE_DB" --output "$PRIVATE_DIR/5524-plan.json"

# 同一 plan 分别安全复制到两个节点；HK GPU 同时只读验证原维护证据和旧 release。
python3 scripts/reconcile_x_5524_prepost.py node-evidence \
  --plan "$PRIVATE_DIR/5524-plan.json" --node-id "$NODE_ID" \
  --outbox "$RECEIPT_DB" --output "$PRIVATE_DIR/5524-$NODE_ID.json"

# 两个证据都须新鲜；此步骤仅由主部署流程在复核后执行。
python3 scripts/reconcile_x_5524_prepost.py apply \
  --plan "$PRIVATE_DIR/5524-plan.json" \
  --node-evidence "$PRIVATE_DIR/5524-cpu-main.json" "$PRIVATE_DIR/5524-hk-gpu.json" \
  --backup-dir "$PRIVATE_DIR/backups" --actor 'codex:user-authorized-20261103' \
  --output "$PRIVATE_DIR/5524-applied.json"
```

每个输出路径必须尚不存在；中断续跑使用新的输出文件。该入口不保证下一代发布成功，发布结果仍需通过正常回执对账。新的 unknown 继续隔离。

## 本地验证

使用真实 SQLite 和现有 claim/reserve/initialize 逻辑，外部网络为离线替身。专项覆盖：原证据永久保留、1→2 授权仅消费一次、重复执行不重置、三个提交边界中断/回滚、两节点新增或 pending/success 回执、原 ACK 内容/版本/PID 漂移、冻结正文/新增 Post/活动租约、授权字段或额外授权漂移，以及备份期间证据过期。无测试帖或生产写入。

```bash
python -m unittest scripts.test_x_5524_prepost_reconciliation scripts.test_x_prepost_recovery scripts.test_pipeline_x_native scripts.test_pipeline_adapters scripts.test_pipeline_x_prepared_publish_recovery scripts.test_pipeline_x_preparation_recovery -q
```

本文件记录实现和本地验收；生产 plan/apply、备份位置与唯一发布回执由主部署流程补充。

2026-11-03 本地组合套件 84 项通过，其中精确对账专项 11 项；两个新 Python 文件编译通过。
