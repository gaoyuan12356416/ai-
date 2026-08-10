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
