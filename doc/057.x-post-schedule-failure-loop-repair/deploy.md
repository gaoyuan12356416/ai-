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

## 验证步骤

- 全部离线 X 测试通过，`git diff --check` 通过。
- Sidecar active、GPU 18820 profile v5 healthy。
- Run 345 不再出现周期性 `x_post_pool_fifo_conflict`。
- Run 274 audit/queued/reserved 各 14，绑定和 episode 不变，`x_write_attempted=false`。
- 自然 timer 执行时只发布 frozen queues；不额外创建测试 Post。

## 回滚方案

1. 停止 schedule/claim timers 与当前 schedule service。
2. 原子切回部署前 symlink，重启 OAuth sidecar。
3. 新增 SQLite 列/审计表保留，不做破坏性逆迁移；旧代码会忽略。
4. 若短剧 recovery 已 apply，不删除 audit、不回写旧 URL；保持 timers 停止并按 ledger 人工核对。

## 注意事项

- 不复制 `.env`/Token 到仓库或报告。
- plan outcome unknown 时禁止盲重试。
- 恢复 CLI 的媒体修复不是 X Post；自然发布结果需以后续 ledger 为准。
