# 部署与回滚

目标 CPU 43.166.187.96，脚本 /root/codex_test，实际数据均在 /mnt/data-disk。
从 GitHub 拉取本分支精确提交，受影响代码、AGENTS.md、crontab、latest/index 先备份到数据盘发布审计目录。保持既有日程与 flock 路径；新增小时级 nice/ionice 管理池回收，不发送业务消息。

服务器先运行全部测试、compileall、storage check，再在 /tmp/tt_minis_multi_dim_dashboard.lock 下安装两个脚本与工作区规则。用实际缓存建立首版索引并校验内容，再验证相同缓存零分区重写。回收旧副本必须先生成执行前清单、扫描消费者和 FD，再应用冻结清单；保留回执。

回滚：在相同报表锁和快照生命周期锁下，从本次 backup 恢复两个脚本、AGENTS.md，删除新增清理 cron 行。不要恢复整个旧 crontab 覆盖他人变更。保留当前有效最新 manifest 和历史分区，旧生成器下一轮能读取并重新全量发布。不要用 mtime 清理共享分区；回滚前暂停发布清理或恢复旧全量 manifest 并重建完整分区。已退休的无引用分析副本不作为代码回滚内容；历史固定引用/证据及在线主库始终保留。

实际提交、备份路径和执行回执记录在交付 verification.md。
