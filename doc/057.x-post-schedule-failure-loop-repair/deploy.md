# 部署文档

## 变更内容

发布 selector、schedule runner、X store、OAuth sidecar、短剧恢复 CLI、Run 274 精确 manifest 和测试/文档。

## 配置项

无 Token、账号、发布时间或 systemd unit 配置变更。沿用 `/etc/x-post-automation.env`、`/etc/x-post-daily.env`、`/etc/x-post-schedule.env` 与现有 GPU repair tunnel。

## 数据库变更

- `x_post_schedule_run.lease_heartbeat_at`
- `x_post_schedule_run.plan_attempted_at`
- `x_post_material_pool.source_material_language`
- `x_post_material_pool.source_hydrated_at`
- `x_post_schedule_bound_drama_failed_media_recovery_audit` 及不可变触发器/索引

必须先对 `/var/lib/x-post-automation/accounts.sqlite3` 做 SQLite online backup，并验证 backup `quick_check=ok`、FK=0。

## 部署步骤

1. 保持 `x-post-schedule.timer`、`x-post-schedule-claim.timer` inactive。
2. push GitHub commit；CPU 服务器仅 fetch 该 exact commit。
3. 从 exact commit 建新 release 目录，保留旧 symlink 目标作为回滚点。
4. 对备份副本先运行迁移和全测试，再切换 `/opt/x-post-automation/current`。
5. 重启 OAuth sidecar，检查 `/health`、DB quick_check/FK、当前 commit/文件 hash。
6. 对 Run 274 manifest 先运行 recovery CLI 默认 validate-only；全部 14 条通过后再 `--apply`。
7. 只读确认 Run 345 仍 0 queue/log/unknown；恢复两个 timer，让新版本按原冻结计划自然建队列并发布，禁止手工制造测试 Post。

## 2026-08-26 生产门禁记录

- 备份：`/var/backups/x-post-automation/20260826T123000CST-before-561da59`，SQLite backup `quick_check=ok`、FK=0。
- 首版部署提交 `baac99aff6213f6204d4406c4a5616c2cd0373a9`；服务器既有 X 测试 796/796 通过。
- Run 274 standalone validate-only：14/14，通过，报告 SHA256 `74b020d458183bd6eba205a96b6bbe481796ae3682851f4a7b1eda4a88bd937e`，DB/X 写入均为 0。
- 首次 apply 在 queue trigger 处失败；SQLite 事务完整回滚，audit=0、修复 URL=0、Run 274/345 原状态不变、DB `quick_check=ok`、FK=0、timers inactive。
- BUG-005 最终提交 `18d9cfb68ee330db633b769112f0a50e38bc3e7c`：独立审查 P0/P1=0；Linux 800/800，日志 SHA256 `764c73f77e056ec58e3658e2ad14fe05748e2f35da69d11ca656d76ea4917b50`。
- 备份副本生产同形态 apply：14/14、audit=14、relay=10、queue 总数不变、Run 345 不变、quick=ok、FK=0；报告 SHA256 `83fe8f4eb9c5b729c8562e2ebe14af76ebed0ba120631cf42f5403012b188124`。
- live standalone validate-only：14/14、updated=0、X write=false、业务行零变化；报告 SHA256 `74b020d458183bd6eba205a96b6bbe481796ae3682851f4a7b1eda4a88bd937e`。
- live apply 前快照：`accounts-before-live-apply-18d9cfb68ee330db633b769112f0a50e38bc3e7c.sqlite3`，SHA256 `ef7aa5a324e7032e5600d9ac2c4c3cbc182d300a37e90e9a90213dcebd76a922`。live apply 14/14，Run 274=`running,16,16,2,0,0`，Run 345/queue 总数不变，X write=false，报告 SHA256 同备份副本 apply。
- 14:43 自然 timer 验收：恢复的 14 条中 13 条一次发布成功；`129.6/109.533/136.32/117.3/105.877/52.181` 等短 relay 均通过媒体上传与 relay 状态机，`invalid_media_dimensions`/trigger 冲突未复现。queue 533 的 source Post 成功后，目标账号 8 Repost 被 X 以 HTTP 403 暂时锁定拒绝；attempt=1、unknown=0、终态 failed，未自动重试。
- 15:00 安全暂停：Run 274=`completed_with_errors, published=15, failed=1, unknown=0`；Run 345 仍 `claimed`、`plan_attempted_at=''`、queue=0、X 尝试=0。schedule/claim timers inactive，sidecar 与 manual timer 保持 active。

## 验证步骤

- 全部离线 X 测试通过，`git diff --check` 通过。
- Sidecar active、GPU 18820 profile v5 healthy。
- Run 345 在观察期间未再写入一次 `x_post_pool_fifo_conflict`；当前因账号 8 锁定停在零计划写入安全点，解锁后再完成 live 计划验收。
- Run 274 audit/queued/reserved 各 14，绑定和 episode 不变，`x_write_attempted=false`。
- 自然 timer 只消费 frozen queues；没有额外创建测试 Post。
- 账号 8 解锁前不得恢复 schedule/claim timers，也不得重跑 queue 533 的 source Post。解锁后必须先核验目标身份，再走保留既有 `source_post_id` 的 repost-only 审计恢复；禁止复用 pre-X 媒体 recovery CLI。

## 回滚方案

1. 停止 schedule/claim timers 与当前 schedule service。
2. 原子切回部署前 symlink，重启 OAuth sidecar。
3. 新增 SQLite 列/审计表保留，不做破坏性逆迁移；旧代码会忽略。
4. 若短剧 recovery 已 apply，不删除 audit、不回写旧 URL；保持 timers 停止并按 ledger 人工核对。
5. 自然发布已经开始后禁止整库恢复 live-apply 前快照，否则会丢失已成功的 X Post ledger；只能回切代码 symlink，并以当前 ledger/X readback 做逐项补偿。

## 注意事项

- 不复制 `.env`/Token 到仓库或报告。
- plan outcome unknown 时禁止盲重试。
- 恢复 CLI 的媒体修复不是 X Post；自然发布结果需以后续 ledger 为准。
