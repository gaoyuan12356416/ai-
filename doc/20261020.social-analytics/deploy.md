# 部署与回滚

源库 gaoyuan12356416/ai-，分支codex/social-analytics-20261020。服务器43.166.187.96，主API /root/drama_material_service；X /opt/x-post-automation/current；日报 /opt/youtube-publisher-daily-report/current。

先本地测试和真实快照演练，提交push；服务器fetch并checkout明确SHA，执行 python3 scripts/deploy_social_analytics.py stage --sha SHA。stage比对deploy/social-analytics-baseline.json并创建数据盘release/backup。再 deploy --backup BACKUP。

部署暂停原活跃调度，等待在途任务结束，取得X runner/scheduler共享锁和日报锁；停止X入口，在线备份SQLite、清单保存旧新HTML；添加tenant列；主API兼容解析部署后迁移链接；权限逐行清单，静态导航和日报切换，相关服务启动、健康/鉴权读回后恢复原触发器。失败保留现场和暂停状态供恢复，不自动重新发布。

回滚命令 python3 scripts/deploy_social_analytics.py rollback --backup BACKUP。逐条清单恢复原HTML/日志，保留新增发布与送达回执，不恢复旧数据库。X保留兼容新旧链接的代码，回退生成开关；旧导航/主API/日报代码和权限按备份恢复。权限上线后变化时拒绝覆盖，需按manifest对账。

备份目录 /mnt/data-disk/social-analytics/backups/<UTC>-<commit> 中 deployment.json 记录准确SHA、状态、服务/触发器、链接迁移hash和逐条结果。备份含私有状态，权限0700，不入Git。
