# 账号范围波动时保留未写计划的定时批次

1036 在 2026-11-03T11:41:47Z、1034 在 11:44:28Z 经三次账号范围复核后失败。已有发布不断完成，临时占用解除，导致长时间素材扫描前后的可用账号范围变化。二者尚未调用 plan：plan_attempted_at、started_at 为空，队列及全部结果计数为零，原素材快照上界仍为 4444。

runner 保留单 tick 最多三次重建的上限。只有该循环自己产生的 ScheduleAccountScopeChanged 可以转为 waiting_account_state；其他同名错误码、上游错误、未知写入和已有队列不走这个分支。耗尽时重新读取原批次，核对完整冻结身份、素材快照、claimed、零计数、空写入标记和空队列；正常续租的回执也必须保留这些字段。通过后不调用 record-failure、不调用 plan，下一自然 tick 从原冻结批次继续。读取失败、状态或身份漂移时只停止当前 tick 并报告，保留原账本；正常下一轮仍先检查写入标记和既有队列。

若批次第一次素材读取前尚未冻结快照，则在第一次扫描后捕获服务端冻结上界，要求原身份和零写入状态一致，后续三轮不得切换上界。FIFO、容量证明、正常写前检查及已有队列的不可重选限制均不变。未增加跨 tick 候选缓存，避免复用已经失效的账号/素材占用证明。

`recover_x_schedule_scope_churn.py` 仅接受已读出的 1034/1036 两次具体失败，按原 run 完整 SHA256、冻结身份 SHA256、首次恢复审计行 SHA256、具体失败时间和快照 4444 核验。第二次恢复写入独立 `x_post_schedule_scope_churn_resume_audit`；首次 `x_post_schedule_zero_write_resume_audit` 完全不修改。apply 先备份，再 BEGIN IMMEDIATE 内复核并 CAS failed_preflight→claimed；不得有队列或写入标记，正常账号验证和素材选择仍由下一 tick 执行。已 apply 的同计划重复调用为 no-op，不重置后续进展。1039 未预先加入未来失败许可。

恢复前先从 GitHub 部署新的 runner 文件；helper 的 plan/apply 都验证实际文件与指定完整 Git commit 的 blob 一致且具备等待修复。只拷贝已提交的精确 runner 文件，不重启或替换正在运行的扫描 PID，下一进程自然采用新逻辑。

```bash
python -B scripts/recover_x_schedule_scope_churn.py plan --db /var/lib/x-post-automation/accounts.sqlite3 \
  --run-ids 1034,1036 --repair-commit FULL_COMMIT --runner-path DEPLOYED_RUNNER --repository GIT_SOURCE --output PLAN.json
python -B scripts/recover_x_schedule_scope_churn.py apply --db /var/lib/x-post-automation/accounts.sqlite3 \
  --plan PLAN.json --backup-dir PRIVATE_BACKUPS --actor OPERATOR --reason ORIGINAL_ZERO_WRITE_RECOVERY --output RESULT.json
```

测试覆盖等待后下一 tick 只写一次计划、首次冻结快照、身份/快照/写入/队列/未知漂移、读取或续租失败、非本循环错误不得等待、首次审计保留、二次精确范围、将来同码失败拒绝、部署文件漂移拒绝、备份后 CAS 漂移拒绝及重复 apply 无重置。生产阶段本次只读核对，无恢复写入。
