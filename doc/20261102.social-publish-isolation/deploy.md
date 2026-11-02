# 部署与回滚
服务器 43.166.187.96。GitHub 分支 codex/social-publish-isolation-20261102。

运行 scripts/deploy_social_publish_isolation.py prepare，记录其 backup；apply --backup PATH 可重复轮询 drain 阶段。仅暂停 runner 主进程，sidecar 完成请求落库后方可停止调用方/切换。新 X release 放在 /mnt/data-disk/social-publish-isolation-20261102/x-COMMIT；FB 只替换 runner；TT 只替换 core；主 API 同步 X service/manual_preflight/personal core。

配置：X Auto 4 workers、每槽最多 20 次阶段调用，每轮 900 秒后停止新领取；素材池和短剧池最多 2 批同时、每批 2 个发布槽，手动独立锁和 2 槽。共享账号由 canonical fence 串行。原未启用 timers/path 不启用。

回滚：先停本次 manifest.triggers 中的活动触发器和 x-video-pool-scheduler.service；让 RUNNERS 和 sidecars 当前请求自然结束（不得强杀上传）；确认所有 RUNNERS inactive 后，在同一源码 checkout 执行 python3 scripts/deploy_social_publish_isolation.py rollback --backup PATH。它恢复旧 X 链接、外部代码/配置和原触发器状态。不得恢复 SQLite 快照或 tokens。备份路径和实际 commit 在生产 manifest 和用户验收报告中记录。
