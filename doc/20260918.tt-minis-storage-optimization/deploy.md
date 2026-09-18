# 部署与回滚

目标 CPU 43.166.187.96，脚本 /root/codex_test，实际数据均在 /mnt/data-disk。
从 GitHub 拉取本分支精确提交，受影响代码、AGENTS.md、crontab、latest/index 先备份到数据盘发布审计目录。保持既有日程与 flock 路径；新增小时级 nice/ionice 管理池回收，不发送业务消息。

服务器先运行全部测试、compileall、storage check，在快照生命周期锁下备份并原子安装脚本和工作区规则；已运行任务保留内存中的旧代码。发布验收另在 /tmp/tt_minis_multi_dim_dashboard.lock 下等待正常任务结束后执行。用实际缓存建立首版索引并校验内容，再验证相同缓存零分区重写。回收旧副本必须先生成执行前清单、扫描消费者和 FD，再应用冻结清单；保留回执。

回滚：在相同报表锁和快照生命周期锁下，从本次 backup 恢复两个脚本、AGENTS.md，删除新增清理 cron 行。不要恢复整个旧 crontab 覆盖他人变更。保留当前有效最新 manifest 和历史分区，旧生成器下一轮能读取并重新全量发布。不要用 mtime 清理共享分区；回滚工具先收集当前及兼容期内 manifest 的所有分区，并把这些文件的 mtime 宽限期重新设为 24 小时，再恢复旧生成器，以兼容旧版按 mtime 清理的行为。已退休的无引用分析副本不作为代码回滚内容；历史固定引用/证据及在线主库始终保留。

实际提交、备份路径和执行回执记录在交付 verification.md。

可执行回滚工具：ops/tt-minis-native-growth/rollback_storage_optimization.py --release /mnt/data-disk/tt-minis-storage/deploy/storage-optimization-20260918。默认仅检查；显式加 --apply 才恢复代码和移除本次新增 cron。该工具要求线上源码仍与本次部署 SHA256 一致，并保留当前 manifest、在线数据库及所有其他定时任务。
