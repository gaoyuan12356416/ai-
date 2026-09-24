# 部署与回滚

部署前推GitHub分支codex/youtube-analytics-report-20260924，再从CPU现有Git镜像fetch该提交到数据盘独立release。运行scripts/deploy_youtube_analytics.py --check并通过Linux测试后执行部署。

上线对象：43.166.187.96:/root/drama_material_service 和 /usr/share/nginx/html。数据盘UUID校验，至少512MiB空间。备份目录/mnt/data-disk/deploy/youtube-auto-publish/backups/analytics-时间-commit，包含manifest、原代码/导航和在线SQLite备份。仅重启drama-material-api.service，自动发布worker、统一writer不重启。

回滚：同一release下python3 scripts/deploy_youtube_analytics.py --rollback <backup>。校验当前SHA与部署manifest一致，拒绝覆盖后续变动；只恢复代码与导航，保留当前数据库、短链、资产、发布/预约及投递事实。无需数据库迁移、新timer或Nginx配置变更。

验收需要匿名401、真实会话只读报表/筛选/export、公共静态字节匹配、worker PID保持及API健康。具体commit/backup/检查结果后续写入deployment-evidence.md。
