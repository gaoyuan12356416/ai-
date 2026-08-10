# 部署与回滚

## 部署

1. 本地测试通过后提交并推送 GitHub，记录精确 commit。
2. 重新读取线上模板版本、未完成批次和未知结果；存在非终态时停止部署。
3. 暂停 schedule 和 claim timers，在线备份 SQLite、token 文件、当前 release 和主服务目标文件。
4. 在数据库副本上先执行迁移，确认 `integrity_check=ok`、旧配置仍为 fixed、模板/版本/账号/历史计数不变。
5. 从 GitHub 精确 commit 构建不可变 release；同步主后端和两个静态页面。
6. 只重启 X sidecar 与 drama-material-api，再恢复 timers。
7. 验证健康端点、页面、静态 hash、数据库计数和自然 timer 日志；不手工触发真实发帖。

## 回滚

- sidecar symlink 切回部署前 release；主服务和静态页面从备份恢复。
- 重启同一组服务并恢复 timers。
- 新增表和列为 additive，可保留；线上出现新写入后不回滚整个 SQLite，避免丢失队列、日志和 token 状态。

## 2026-08-10 生产记录

- 代码 commit：`0d36c7b56b8b415a1ab5776249540c5a7c0e8fb6`。
- 当前 release：`/mnt/data-disk/x-post-automation/releases/0d36c7b56b8b415a1ab5776249540c5a7c0e8fb6`。
- 回滚包：`/mnt/data-disk/x-post-automation/backups/20260810T064002Z-random-schedule-0d36c7b`，manifest 校验通过。
- 前一 release：`/mnt/data-disk/x-post-automation/releases/dafbd174104c2f4ee1cb6a99725e6929f6b4abca`。
- 迁移前后保持 32 个 schedule run、150 个 queue、150 个 publish log；配置版本、账号、固定时间和模板逐字段不变，`integrity_check=ok`。
- sidecar、主 API、两个 timer 均 active；两个线上页面 HTTP 200。自然 claim 为 0，自然 scheduler 为 `no_due`，未触发真实发帖。
