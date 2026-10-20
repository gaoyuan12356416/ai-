# 部署与回滚

源库 gaoyuan12356416/ai-，分支codex/social-analytics-20261020。服务器43.166.187.96，主API /root/drama_material_service；X /opt/x-post-automation/current；日报 /opt/youtube-publisher-daily-report/current。

先本地测试和真实快照演练，提交push；服务器fetch并checkout明确SHA，执行 python3 scripts/deploy_social_analytics.py stage --sha SHA。stage比对deploy/social-analytics-baseline.json并创建数据盘release/backup。再 deploy --backup BACKUP。

部署暂停原活跃调度，等待在途任务结束，取得X runner/scheduler共享锁和日报锁；停止X入口，在线备份SQLite、清单保存旧新HTML；添加tenant列；主API兼容解析部署后迁移链接；权限逐行清单，静态导航和日报切换，相关服务启动、健康/鉴权读回后恢复原触发器。失败保留现场和暂停状态供恢复，不自动重新发布。

回滚命令 python3 scripts/deploy_social_analytics.py rollback --backup BACKUP。逐条清单恢复原HTML/日志，保留新增发布与送达回执，不恢复旧数据库。X保留兼容新旧链接的代码，回退生成开关；旧导航/主API/日报代码和权限按备份恢复。权限上线后变化时拒绝覆盖，需按manifest对账。

备份目录 /mnt/data-disk/social-analytics/backups/<UTC>-<commit> 中 deployment.json 记录准确SHA、状态、服务/触发器、链接迁移hash和逐条结果。备份含私有状态，权限0700，不入Git。

## 实际部署结果（2026-10-20）

状态 deployed_triggers_restored；主API、X sidecar、X Auto均active，Nginx配置检查及reload成功。代码提交e3dd1346d0bed2ce13d21be48d22eff9e5aac8cc，部署依赖修正40c756ca8401f04137414a47c555f84baa63ea82。报告包补齐GitHub已有titles.py后，systemd受限沙箱预览通过，未发送日报。

实际备份：/mnt/data-disk/social-analytics/backups/20261020T075521Z-e3dd1346d0be。4000条线上迁移manifest SHA db2580ffbb8a90fbad76937ed7eefd84c64d7ba85e4151930b4404433b4de4af；47个旧发布/自动化表摘要保持一致。默认248合入郜远，模板创建人/手动提交人已冻结。

准确回滚命令（在43.166.187.96执行）：

    python3 /mnt/data-disk/social-analytics/source/scripts/deploy_social_analytics.py rollback --backup /mnt/data-disk/social-analytics/backups/20261020T075521Z-e3dd1346d0be

回滚只作用于清单内链接、原权限和配置，不恢复整库。新发布事实和日报回执保留。旧记录出现新的状态变化时条件检查会拒绝覆盖，需按清单人工对账。

线上入口：https://ai.yingliangads.com/social-analytics.html 。下一次社媒日报北京时间2026-10-21 11:00；本次无真实测试Post、无测试或补发群消息。静态数据页桌面/390px已验收；日报HTML预览只检查内容，未冒充飞书客户端最终渲染验证。
