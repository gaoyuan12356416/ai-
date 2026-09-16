# 部署与回滚

入口 `scripts/deploy_youtube_share_x.py --commit <GitHub精确commit> [--check]`。固定基线哈希在脚本中。本需求独立worktree，用户原目录未提交改动不动。

主站只替换app、YouTube service/bridge、X client/text helper、三份静态资源；静态双目录同步。Sidecar完整复制当前生产25MB release，再覆盖4份功能文件，保留现有duration routing和自动发布依赖。

台账 `/mnt/data-disk/x-post-automation/youtube-shares/ledger.sqlite3`，目录服务用户0700、文件0600，挂载UUID强校验。备份在 `/mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-share-x-*`，含文件哈希/SQLite在线备份/manifest。

短暂暂停当前活动X manual/schedule/claim/auto runner/auto scheduler触发器，确认无发送中任务后切换；只重启API和X OAuth sidecar，YouTube worker保持运行。finally恢复此前活动触发器。不清空原台账。

回滚用同一发布脚本 `--rollback <backup>`；校验当前版本未漂移且无待处理分享，恢复主文件并切回旧sidecar，保留所有数据库。健康检查失败自动回滚。具体commit和验证记录见release-result.md。
