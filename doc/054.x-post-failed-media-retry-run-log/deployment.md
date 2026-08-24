# 部署与回滚

## 运行提交

- Git branch: `codex/x-failed-media-retry-run-log-20260824`
- Runtime commit: `3b9cda698e8f4ad1a025a8f9e2e1dd6296c95769`
- GPU repair commit: `cd3811bc042a9c89fcb11e5f50ad63234d7f1245`
- Background recovery command commit: `a68ca7b684a1850100fd3102cb02c081eae47dbf`
- Immutable release: `/mnt/data-disk/x-post-automation/releases/3b9cda698e8f4ad1a025a8f9e2e1dd6296c95769`
- GPU immutable release: `/data/x-post-media-repair/releases/cd3811bc042a9c89fcb11e5f50ad63234d7f1245`

## 备份

- `/mnt/data-disk/x-post-automation/backups/20260824T1658+0800-failed-media-retry-run-log-pre-3b9cda698e8f4ad1a025a8f9e2e1dd6296c95769`
- `/data/x-post-media-repair/backups/20260824T1732+0800-cos-upload-retry-pre-cd3811bc042a9c89fcb11e5f50ad63234d7f1245`
- 包含线上 SQLite、迁移演练副本、当前 release 指针、被覆盖的 backend/static/nginx 文件、服务与 timer 状态及 SHA-256 清单。

## 部署步骤

1. 停止 schedule、claim、manual 与 X Auto 发布 timers，等待 oneshot 自然结束。
2. 校验无 active schedule/manual、无 unknown outcome，SQLite 健康。
3. 原子切换 `/opt/x-post-automation/current` 到精确 Git commit。
4. 同步 `features/x_posts/service.py` 到主后台，同步 `x-post-logs.html` 到后台与 Nginx 静态目录。
5. 重启 `x-post-automation.service`、`drama-material-api.service`。
6. 验证 combined runs API、文件 SHA、SQLite、服务状态。
7. 恢复原 timers；`x-post-daily.timer` 保持 masked/inactive。

## 一次性恢复下发

- systemd unit：`x-post-failed-media-recovery-20260824.service`
- 范围：schedule runs 318、320，共 21 条失败队列，只允许 `failed -> pending` 一次。
- 检查点：下发时视频已完成 6/20，图片 1/1 已重制；后台任务从检查点续跑。
- 后台脚本会在全部素材预检合格后暂停五个现行 timer、等待 oneshot 清空、备份 SQLite、对两个 run 先 validate-only 再 apply，随后仅下发一次 `x-post-schedule.service` 并恢复 timer。
- 按操作要求，仅确认 unit 成功进入 `active/running`，不阻塞等待最终外部发布结果。

## 回滚

1. 停止相同 timers 并等待 oneshot 结束。
2. 将 `/opt/x-post-automation/current` 原子切回备份记录的 release。
3. 从备份恢复主后台 `service.py` 与两份静态页面。
4. 如尚未执行恢复 apply，可直接恢复备份 SQLite；如已经产生真实 Post，不允许用数据库回滚掩盖外部结果，必须保留新台账并人工对账。
5. 重启两个服务，复核 SQLite 与 API 后恢复 timers。
