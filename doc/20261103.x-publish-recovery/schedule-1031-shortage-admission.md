# 1031 原 slot 的一次性依赖恢复

1031（2026-11-03 08:44，冻结配置 27 / 35 个账号）在 12:50:31Z 因
`x_post_schedule_drama_shortage` 结束，零 queue、零 plan_attempted。其原失败全行、
首次授权、另一维护 actor 于 12:45:17Z 写入的第二次 episode 审计均固定 SHA256。
只读核验显示，1029 的 19 条队列 5633–5651 占据同批账号的当前剧集；其成功回执才能
按现有正常逻辑推进集数。另两个账号较晚结束了独立在途任务，不能因此提前只建两个账号。

`scripts/recover_x_schedule_1031_shortage.py` 仅支持这一次事件，所有生产操作均由主维护流程决定。

- `plan` 和 `wait-plan` 只读两个 SQLite 库，不实例化会迁移数据库的 Store，不调用网络发布接口。
- 固定 19 条依赖的完整冻结身份；全部进入已知终态才允许 ready。成功须原生 Post 回执与中央 publication 一致；确定失败须原生 unknown=0、无 Post/Repost 许可、双账本一致、中央已结束且无 lease/resource。存在 Post 许可的失败留待独立回执核对，此工具不猜测。
- 等待中保留 1031 的 failed_preflight。每轮复核原失败及两次审计；另一 actor 的任何改动立即拒绝。只等待正常依赖进展，最长 24 小时，每轮 1–60 秒。
- 19 条结束后调用当前部署版本的只读 `_drama_assignment_candidates`，要求至少一项有效本地候选，保存全部冻结账号的候选/未选原因。普通 runner 仍须完成来源、账号、语言、媒体等正常预检；不要求重选其他剧或覆盖所有 35 个账号。
- `apply` 必须显式提供 ready plan 的 SHA256。备份后，在原生 `BEGIN IMMEDIATE` 内重核完整快照，追加第三审计 `x_post_schedule_1031_shortage_resume_audit`，把原 slot 改为 claimed。提交前再次读取中央终态。数据库原有首次授权允许普通 runner 获取该原 slot。
- 保留所有冻结日期、账号、模板、snapshot、原 plan 字段和前两次审计；不新建业务 slot、不推进 cursor、不清 unknown、不调用 plan/publish。第三审计拒绝 UPDATE、DELETE 和 INSERT OR REPLACE。相同 plan 重复 apply 为 no-op，绝不重置下一次执行。

受控临时 oneshot 可只运行下列只读等待。`DEPLOYED_COMMIT` 必须是三个实际部署文件
（service、account_blockers、runner）的完整 Git SHA；工具同时核对本地只读 selector 的字节。
`SERVICE_PATH` 指实际当前部署的 service.py，不能用仓库副本冒充 runtime。
输出及 `.events.jsonl` 为 0600 且不覆盖，逐轮 fsync 事件，拒绝/超时会留下证据。

```sh
python3 -B scripts/recover_x_schedule_1031_shortage.py wait-plan \
  --native /var/lib/x-post-automation/accounts.sqlite3 \
  --central /mnt/data-disk/pipeline-dispatch/dispatch.sqlite3 \
  --commit "$DEPLOYED_COMMIT" --service-path "$SERVICE_PATH" \
  --repository /mnt/data-disk/x-publish-recovery-20261103/source \
  --wait-seconds 86400 --poll-seconds 30 --output "$PRIVATE_DIR/1031-wait.json"
```

等待只产出计划，不会自动 apply。只有输出 `result.snapshot.state=ready`，经主维护流程核对
`result.plan_sha256` 后，才能执行一次准入；超时得到 waiting plan 不可 apply。

```sh
python3 -B scripts/recover_x_schedule_1031_shortage.py apply \
  --plan "$PRIVATE_DIR/1031-wait.json" --expect-plan-sha256 "$REVIEWED_PLAN_SHA256" \
  --backup-dir "$PRIVATE_DIR/backups" --actor codex_x_recovery_20261103 \
  --reason user_authorized_original_slot_after_1029_terminal \
  --output "$PRIVATE_DIR/1031-apply.json"
```

若 ready 到 apply 之间候选或依赖改变，CAS 拒绝；重新只读 plan 后复核，不循环 apply。
普通 runner 后续再次失败不会由本工具重新准入。维护者不应同时人工重置 1031 或这 19 条依赖；
工具不增加跨服务全局锁，只以原生事务和最终中央复核检测漂移。

离线验证：`python -m unittest scripts.test_x_schedule_1031_shortage_recovery`。
覆盖 19 个任一未完成、unknown/lease/resource/回执或账本不一致、确定 prepost 失败与 Post intent、
外 actor 改动、无候选、正常账号 blocker、备份后漂移、提交前中央漂移、回滚、审计不可变、
重复 apply、其他 slot/篡改 plan、部署漂移及只读等待到期/结束。无生产写入或发布测试。
