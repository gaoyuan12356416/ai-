# 部署与回滚

目标 CPU 43.166.187.96。协调器 /opt/pipeline-dispatch/current 实际为 /mnt/data-disk/pipeline-dispatch-runtime/x-publish-recovery-a97fc23/candidates/coordinator。
静态目录 /root/drama_material_service/static 与 /usr/share/nginx/html。
基线 views.py 与静态文件都逐字节匹配 b58a5257ff43ccd7edbfc19f8eb2cd95c312a809；其他线上 core/service 有覆盖修复，禁止整树替换、禁止 checkout 共用 Git 仓库。

使用 scripts/deploy_pipeline_queue_states.py --sha TARGET_SHA --expected-sha b58a5257ff43ccd7edbfc19f8eb2cd95c312a809，默认只读预检。先本地提交 push，再服务器 fetch 精确 SHA 并取出该版本脚本；通过预检后加 --apply。校验数据盘 UUID，保存目标文件备份和 manifest；不覆盖数据库、配置、凭据或执行器文件。

仅协调器 18846 新连接短暂阻断，独立 75 秒 timer 清除自身规则；等 HTTP 线程排空后停止协调器，原任务在执行进程内继续。停止/启动限制总耗时，失败回滚当前修改文件。三份静态依赖先 JS/CSS 后 HTML。验证 /health、公开三文件、API 和 CPU 节点主 PID 未变化、容量指纹未变化、其他运行代码未变化。

回滚（BACKUP 为部署结果中的绝对路径）：
`/usr/bin/python3.9 BACKUP/deploy.py --rollback BACKUP --apply`
该命令先校验现有文件与部署 manifest 匹配，采用相同 HTTP 排空流程，只回退本次文件。不能恢复旧数据库或发布事实，不能重置任务。
