# 回滚运行手册

触发条件：六 API/权限异常、recipe 或 wrapper identity 漂移、migration 非零二次 dry-run、GPU manifest/render 偏差、tunnel/health 失败、YouTube processing/unknown 防重失效、统一 outbox 错误写成功。

1. 立即令 `YOUTUBE_LIVE_ENABLED=0` 并隐藏/阻断发布入口；这不授权删除或修改任何外部视频/评论。
2. drain CPU drama 队列；对 `submitted/processing/unknown` 任务保留账本并停止旧 worker 接管，禁止重传。
3. CPU 依 GitHub-first 回到已记录 SHA；关闭 unified sync 并保留 additive MySQL 列/索引，常规回滚绝不恢复共享 CynosDB 整库。CPU SQLite 仅可在确认无新任务且核验一致性备份后单独恢复；CynosDB 云备份只作另行批准的灾难恢复。
4. CPU GPU URL 切回 legacy `127.0.0.1:18787` 后验证；停止新 18788 tunnel。HK `current` 原子指回上一 release，保留失败 release、日志和 manifest 证据。
5. gy location 未切流则无动作；已切流时先移除/禁用新 location。已生成的数字 wrapper 与 SQLite 行不可覆盖/复用/删除。
6. 统一 outbox 停止 worker但保留未同步事件。外部三表或 external IDs 不删除；任何外部修复需独立 owner 和授权。

完成回滚需记录 CPU/HK SHA、unit/tunnel/health、SQLite integrity、队列状态、processing/unknown 数、gy namespace 状态及所有未完成外部协调项。
