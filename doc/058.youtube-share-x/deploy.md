# 部署与回滚

入口 `scripts/deploy_youtube_share_x.py --commit <GitHub精确commit> [--check]`。固定基线哈希在脚本中。本需求独立worktree，用户原目录未提交改动不动。

主站只替换app、YouTube service/bridge、X client/text helper、三份静态资源；静态双目录同步。Sidecar完整复制当前生产25MB release，再覆盖4份功能文件，保留现有duration routing和自动发布依赖。

台账 `/mnt/data-disk/x-post-automation/youtube-shares/ledger.sqlite3`，目录服务用户0700、文件0600，挂载UUID强校验。备份在 `/mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-share-x-*`，含文件哈希/SQLite在线备份/manifest。

短暂暂停当前活动X manual/schedule/claim/auto runner/auto scheduler触发器，确认无发送中任务后切换；只重启API和X OAuth sidecar，YouTube worker保持运行。finally恢复此前活动触发器。不清空原台账。

回滚用同一发布脚本 `--rollback <backup>`；校验当前版本未漂移且无待处理分享，恢复主文件并切回旧sidecar，保留所有数据库。健康检查失败自动回滚。具体commit和验证记录见release-result.md。

2026-09-17 短链宏增量用 `scripts/deploy_youtube_share_short_url.py --commit <GitHub精确commit> [--check]`：校验当前已部署的四份源文件，只更新主站 bridge/text helper 和 JS/HTML，静态资源双目录同步。先备份六个目标文件，再仅重启主 API。X sidecar 使用的 URL 校验/权重函数无改动，保持其 release 和运行进程，YouTube worker 同样持续运行。无数据库迁移、短链生成或平台发布。增量回滚入口为同一脚本 `--rollback <本次backup>`，可加 `--check` 仅验证回滚条件。

同日播放器检查增量用 `scripts/deploy_youtube_share_player.py --commit <GitHub精确commit> [--check]`，新增主站 x_card 模块并更新 bridge/text helper/JS/HTML，共七个部署目标。只重启主 API，保留 sidecar 和 worker。备份 kind 为 youtube-share-player；同一脚本支持 `--rollback <backup> [--check]`。没有任务/短链/分享台账迁移，也不通过发布新帖验收。
