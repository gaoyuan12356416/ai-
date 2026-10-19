# 部署与回滚

CPU 43.166.187.96。执行 ops/screenshot-mobo-products-20261019/deploy.py，默认仅预检；--apply 部署。必须由服务器 GitHub 拉取的干净精确提交执行。

备份 /mnt/data-disk/codex/tasks/screenshot-mobo-products-20261019/before-deploy：相关源码、两份静态页面、crontab 和在线 SQLite 备份。部署清单记录每个文件前后 SHA256、提交和图片 worker PID。数据盘 UUID、空间、待处理任务、旧函数 AST、runner hash 都是发布门槛。

仅重启 drama-material-api。四个图片 worker、模型、并发和数据库不变。失败自动恢复文件，后续人工回滚执行同一提交 deploy.py --rollback，保留当前数据库和所有图片。先确认封面及视频任务已结束。

真实验收通过后启用三产品定时入口并运行一次昨天。不得对已接受但响应不确定的请求盲目重发；先回读任务库。

部署分两阶段：--apply 保持旧 wrapper，真实验收通过并写出 acceptance.json 后执行 --activate。回滚会校验文件指纹，遇到发布后的其他改动则拒绝覆盖，需重新审阅局部回滚。
