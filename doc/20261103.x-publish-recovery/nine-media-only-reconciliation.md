# 九条媒体阶段 unknown 的独立对账

用户授权恢复昨日和今日积压，但禁止结果不明直接重发。独立只读核查将以下九个原执行核实为在媒体上传阶段终止、未进入 Post；该结论共同依赖原 worker/PID/版本、原终态 ACK、两库逐条许可与冻结身份，不是从缺少 Post intent 单独推导。

| Queue | 原 log | Publish job | 账号 | Auto task | 原许可数 | 发布节点 |
| --- | --- | --- | --- | --- | --- | --- |
| 5485 | 5431 | 31796 | 13 | 2669 | 60 | HK GPU |
| 5487 | 5433 | 31800 | 2 | 2676 | 23 | HK GPU |
| 5501 | 5448 | 31880 | 11 | 2320 | 104 | HK GPU |
| 5508 | 5454 | 31893 | 42 | 2319 | 23 | HK GPU |
| 5532 | 5471 | 31990 | 78 | 2339 | 18 | HK GPU |
| 5534 | 5472 | 31992 | 38 | 2316 | 18 | HK GPU |
| 5522 | 5474 | 31994 | 15 | 2401 | 85 | HK GPU |
| 5523 | 5473 | 31995 | 45 | 2387 | 134 | HK GPU |
| 5459 | 5392 | 31719 | 102 | 2650 | 1 | CPU |

原始独立核查 manifest 的 SHA256 为 `7bbf74d54aab951ad0dc680ecd20f219de06d39851d421b9442caa7912d601a2`。代码中只固化必要身份、指纹和维护文件位置，未提交原 raw 凭据或完整业务日志。精确事实以 `scripts/x_media_only_incidents_20261103.json` 为准；维护入口核对该清单的语义指纹，不接受任意清单参数。

## 证据链

- 每条均为原 publish generation 1 / attempt 1，父 manual run 是单队列 auto_template 的 needs_review，不是用户取消。原 native/central operations 完全对应，仅 initialize/append；完整队列、日志、claims/history、checkpoint、父任务和冻结成片都绑定审计指纹。
- 原 worker 对应的完整版本为 `95fdaa4c4db0ed19a465cb126d539274e95ea918` 或 `ec8984773c4bc33e832ec5ae483cb3dcad7482dc`，维护前父/子 PID 与版本审计相符。相关 worker、executor 与 X 客户端的真实执行顺序已独立核对：每个 HTTP 请求先获取许可；媒体上传返回并记录 x_media_uploaded 后才创建 Post；失败终态只能在同步执行函数抛错后写入 outbox。
- 各任务恰好 4 个原 ACK，原始 JSON 和去除 claim token 后的 JSON 均有独立指纹，并与中央 delivery/claim 对齐。8 条的 4 个 ACK 在 HK GPU；5459 的 prepare 2 个 ACK 在 HK GPU、publish 2 个 ACK 在 CPU。
- 5459 的原错误为 coordinator_operation_failed，其他 8 条为 pipeline_lease_lost。两类错误都保留原文，不互相替换。历史原始失败仍保留 unknown=true。
- 5485/5487 的维护文件使用 runtime_sha/child_pids_before；其他 GPU 使用 previous_sha/children_before；5459 使用 CPU audit.before.json。入口按清单字段读取精确文件，并复核原文件及 release 内容哈希。

5381/5386 已进入 finalize 路径、5393 历史父链证据不足、5452 缺少完整外部重置证明，均不在本入口范围；不存在通过错误码或“无意图”自动扩大范围的选项。

## 公共维护流程与兼容

`scripts/x_media_only_reconciliation.py` 提取原 5524 的安全流程。旧 `reconcile_x_5524_prepost.py` 保留原入口、常量、plan kind/内容、审计和 journal 表名；旧成功审计与 Auto2395 的固定观察授权不改变。新九条使用独立公共表 `pipeline_x_media_only_no_post_audit` 与 `pipeline_x_media_only_reconciliation_apply`，以原 publish job ID 隔离。

每条 apply：备份两库并核验 → 中央 unknown + hold/隔离下记录 append-only 原证据和 staged journal → 中央写锁下 CAS 原生，保存原 unknown/日志/attempt/冻结内容/历史，写入既有唯一 1→2 授权 → 再复核两库与未消费授权，单中央事务恢复该 job ready、释放该 job 精确账号隔离、标记 committed。attempt 保持 1；正常下一代首次 initialize 才消费一次授权并递增至 2。

每阶段要求两节点新证据在 120 秒内。新增、pending、成功、其他代次或包内容变化都拒绝。中断时中央保留 unknown/hold，必须用同一 plan 和新证据续跑。重复 apply 只返回已应用，不重置后续尝试。审计不允许 UPDATE、DELETE 或 REPLACE；不删除原操作、claim、receipt、delivery 或失败事件。

## 操作顺序

全部生产操作由主部署流程在 GitHub 固定提交检出后执行。一次只选择清单内一个 queue；不停止正常发布，不替换 current 或重启服务。数据库与 Python 路径从当前部署配置读取，HK GPU 使用已验证 Python 环境。

```bash
"$PYTHON" scripts/reconcile_x_audited_media_only.py plan --queue-id "$QID" \
  --central "$CENTRAL_DB" --native "$NATIVE_DB" --output "$PRIVATE_DIR/$QID-plan.json"

# 将同一 plan 复制到两个节点，每个节点读取自己的 outbox 与所需原维护文件。
"$NODE_PYTHON" scripts/reconcile_x_audited_media_only.py node-evidence --queue-id "$QID" \
  --plan "$PRIVATE_DIR/$QID-plan.json" --node-id "$NODE_ID" --outbox "$OUTBOX_DB" \
  --output "$PRIVATE_DIR/$QID-$NODE_ID.json"

"$PYTHON" scripts/reconcile_x_audited_media_only.py apply --queue-id "$QID" \
  --plan "$PRIVATE_DIR/$QID-plan.json" \
  --node-evidence "$PRIVATE_DIR/$QID-cpu-main.json" "$PRIVATE_DIR/$QID-hk-gpu.json" \
  --backup-dir "$PRIVATE_DIR/backups" --actor 'codex:user-authorized-20261103' \
  --output "$PRIVATE_DIR/$QID-applied.json"
```

每条只有完成两库 committed 后，才能使用 `rearm_x_auto_receipt_tracking.py` 对对应精确 Auto task 生成新 plan/apply。该入口同时绑定九条清单中的原 Auto 全行哈希、双库相同原对账 plan、原证据指纹和唯一 1→2 授权；**保留 Auto unknown=1**，只切换 retry_wait/reconcile，观察原 execution IDs。原生新的 unknown、未提交、身份变化或未知任务继续拒绝；唯一真实成功回执才清除 Auto unknown。已成功的原生队列直接交给成功回执 helper，不重排观察。

## 验证

旧 5524 中断/回滚/幂等测试继续通过。新增测试覆盖固定九 ID 清单、错误 queue/manifest 合同拒绝、CPU/GPU 混合 ACK、协调器失败错误原文、RGBA/CPU/GPU 历史 PID 格式、原证据漂移、共享审计防覆盖，以及 Auto 仅查询原队列且 publish/recover/record 调用为零。

实现阶段未生产 apply、未 push、未启停服务；实际计划、备份、版本、恢复数量和唯一 Post 回执由主部署流程验收。

2026-11-03：包含原生恢复、旧 5524、Auto 回执跟踪、publisher/store 与中央桥接的组合 172 项全部通过，编译通过。独立复核确认九条清单、维护文件格式、完整指纹和实际原错误消息均与只读原始证据一致，无阻塞发现。
