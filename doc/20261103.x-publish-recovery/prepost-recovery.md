# 创建 Post 前明确失败的精确恢复

## 适用范围与审查结论

用户已授权恢复北京时间 2026-11-02、2026-11-03 未完成的 X 任务，同时要求先核对成功回执、结果不明不得重发。本工具只处理显式 queue ID，且原生和中央都明确失败的 direct 队列。发布阶段全部历史操作只能是媒体 initialize/append；任何 finalize、Post、Repost、成功回执或历史 unknown 均拒绝。媒体 unknown 即使没有 Post 意图，也必须另行只读核实，不能通过此工具恢复。

零发布尝试的预检/制作失败单独使用 `--stage prepare`。该模式要求所有代次均没有外部操作意图，保留原始请求、随机配方、GPU/CPU profile 和 job key。已准备好的发布任务使用 `--stage publish`，复用原生 claim 中冻结的最终成片、原账号与原正文；中央旧投影可能仍存源片，因此不把中央 artifact 冒充最终成片。

`publish` 同样支持已冻结发布 descriptor、但 initialize 前明确失败的 `prior_attempts=0`。它仍使用逐代授权，并在下一代首次 initialize 原子消费授权、计数 0→1；不留下可再次使用的 armed 授权。

审计同时检查全部中央 source 阶段、claim 历史、原生 claim/operation/checkpoint、成片/随机制作记录、父任务和所有注册执行节点的 receipt_outbox。当前失败必须已稳定至少 120 秒、无租约和资源占用；每个节点证据须在 120 秒内生成，当前 job/generation/error_code 必须有已 ACK 的明确失败回执。Worker 仅在执行函数结束后入队终态失败，因此终态 ACK 加上无租约/无 pending 回执构成该执行已停止的证据。工具不调用 X，不读取 OAuth Token。

## 实现与中断边界

- `features/pipeline_dispatch/x_prepost_recovery.py`：原生逐代授权；校验原队列/正文/成片身份，下一次 initialize 才将原 attempt_count 加 1；授权仅消费一次。
- `features/pipeline_dispatch/adapters.py`：仅携带有效授权的精确队列允许下一代正常调度，真实 attempt_count 始终保留。
- `features/pipeline_dispatch/x_native.py`：已授权下一代保留原生账号、语言、权限、素材与账号并发保护；旧代次在恢复授权写入后立即失效。发布成功日志不会被失败回执覆盖。
- `scripts/recover_x_prepost_failures.py`：只读 plan、每节点只读 node-evidence、带完整数据库备份及 CAS 复核的 apply。

apply 分为三个持久阶段：中央 failed 状态加 `operator_prepost_recovery_pending` → 原生写入逐代授权并恢复 reserved/queued → 中央恢复 ready/queued。中间中央始终不可领取；中断只能用同一 plan 续跑，不能生成新任务或删除历史。旧 attempt_count、started_at、失败原因、操作和回执保留，旧日志完整快照写入审计。中央旧代的 external_intent 移入保留的 operation/审计事实，当前代准备标记置零；generation 在下次正常 claim 时递增。

零尝试 auto_template 的原生父任务仍是已计划任务，因此汇总后将仅该父任务的 queued 状态恢复为 running；不会创建新队列或重做素材选择。其他父任务及兄弟任务仍按原聚合规则处理。

## 运维命令

由发布主流程在部署已验证版本后执行。下面数据库及节点路径必须取当前服务配置，不能用猜测值。计划与备份留在私有目录，不提交 Git。

```bash
python3 scripts/recover_x_prepost_failures.py plan \
  --central "$CENTRAL_DB" --native "$NATIVE_DB" \
  --queue-ids 5525 5538 5560 --stage publish \
  --days 2026-11-02 2026-11-03 --output "$PRIVATE_DIR/prepost-plan.json"

# 将同一计划安全复制到 plan.nodes 列出的每个执行节点，分别执行：
python3 scripts/recover_x_prepost_failures.py node-evidence \
  --plan "$PRIVATE_DIR/prepost-plan.json" --node-id "$NODE_ID" \
  --outbox "$RECEIPT_DB" --output "$PRIVATE_DIR/node-evidence.json"

# 汇总所有节点新证据到数据库所属服务器，再执行精确 apply：
python3 scripts/recover_x_prepost_failures.py apply \
  --plan "$PRIVATE_DIR/prepost-plan.json" \
  --node-evidence "$PRIVATE_DIR/cpu-evidence.json" "$PRIVATE_DIR/gpu-evidence.json" \
  --backup-dir "$PRIVATE_DIR/backups" --actor 'codex:user-authorized-20261103' \
  --output "$PRIVATE_DIR/prepost-applied.json"
```

节点清单以实际计划为准，示例文件数量不代表线上节点数。证据过期可重新生成 node-evidence；plan 在原生或中央实质状态变化后必须重新只读审计。若 apply 已进入 pending 阶段，必须保留并续跑原计划，不能通过清理审计表绕过中间状态。

执行后观察正常调度领取的下一代、原日志计数递增以及唯一 Post ID。出现 unknown 时停止该条后续操作，只观察回执。回滚仅回滚代码，不能把全库备份覆盖回仍在发布的数据库；保留授权、历史操作和新增回执。已进入执行的新代不得撤回或重置。

## QA 与代码审查

本地使用真实 SQLite 中央/原生数据库、真实队列与 claim 流程，X 凭据/网络边界为离线替身，不创建测试帖。

| 用例 | 结果 |
| --- | --- |
| 明确 append 失败后复用完全相同 descriptor，attempt 1→2，唯一 Post | 通过 |
| 同一 initialize 重入不增加计数、不重新发放许可 | 通过 |
| 原生 armed 后中断，中央仍 failed/pending，同一计划安全续跑 | 通过 |
| 恢复后旧失败回执拒绝；已成功后当前代失败不覆盖成功 | 通过 |
| 任意代次 Post/finalize/Repost 意图、unknown/Post ID/media ID 拒绝 | 通过 |
| 任意节点 pending、已 ACK 成功或 unknown 回执拒绝 | 通过 |
| 计划内容漂移、成片漂移、错误下一代次、日期越界拒绝 | 通过 |
| 零尝试预检失败复用随机请求，计数保持 0，无 X 操作 | 通过 |
| 零尝试发布阶段失败，下一代首次上传计数 0→1 且授权消费 | 通过 |

验证命令：

```bash
python -m py_compile features/pipeline_dispatch/x_prepost_recovery.py features/pipeline_dispatch/x_native.py features/pipeline_dispatch/adapters.py scripts/recover_x_prepost_failures.py scripts/test_x_prepost_recovery.py
python -m unittest scripts.test_x_prepost_recovery scripts.test_pipeline_x_native scripts.test_pipeline_adapters scripts.test_pipeline_x_prepared_publish_recovery scripts.test_pipeline_x_preparation_recovery -q
```

2026-11-03：组合 71 项全部通过；随后新增成片漂移/错误代次及零尝试发布用例，恢复独立套件 9 项全部通过，编译与 `git diff --check` 通过。该记录只证明本地行为，线上版本、计划、备份及最终回执由主部署流程验收。
