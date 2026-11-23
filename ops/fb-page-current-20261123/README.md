# Facebook Page 当前收入画像

本目录配套 CPU 主机的专用部署。每次操作从 GitHub 分支取经核对的提交，使用发布目录中的脚本。版本目录为 `/mnt/data-disk/fb-page-current-20261123/code/<完整 Git SHA>/ops/fb-page-current-20261123`。以下命令均从该目录运行。

## 切换与验收

1. 查看 `baseline.json`、现有服务版本、定时器、分配积压、画像错误和只读端延迟。用 `python3 -B operations.py state` 读取 63350 上的已提交版本。
2. 初始部署使用 `operations.py prepare`、`operations.py seed` 和 `deploy.py writer`。新建的候选表主键是 Page ID。定时器继续按原时刻启动刷新。迁移期间，同一个自然刷新把冻结输入交给独立对照任务，原服务的发布回执和新表验证结果分别保存在两个目录。
3. `python3 -B readers_deploy.py candidates` 启动本机备用端口。`python3 -B verify.py start-pin`、`verify.py finish-pin` 验证跨版翻页。`verify.py prepare-selection` 可按最新官方回执重建轻量选页缓存。正常刷新会自动更新缓存。`verify.py compare` 对比逐页字段、金额、实际分配输入、结果、缓存冷读和命中。`observe_reader.py fb_page_revenue_profile_next` 可观测自然提交中的直连 SQL。
4. 核对自然刷新在 `comparisons/<版本>.json` 的逐页结果，经备用端口验收后执行 `python3 -B readers_deploy.py handoff`。流量平滑回到原服务端口。分配补写和通知保持单一后台执行进程。
5. `python3 -B cutover.py cutover` 取得画像刷新锁，以一次改名交换原表和新表。读回生产副本上原名的主键和行数。遇锁冲突或网络中断，按现有切换意图重复相同命令，脚本通过实际表布局判断是否完成。
6. 再等一轮自然发布和旧表追平。运行 `verify.py compare`、`verify.py metrics --label after` 并核对真实业务流量、积压和原有 Meta 告警。`observe_reader.py fb_page_revenue_profile` 用短事务验证调用两次直连 SQL 期间发生真实提交。通过后运行 `python3 -B cutover.py freeze`。
7. `python3 -B history_archive.py` 按冻结版本逐 Page 从 63350 限速导出。每版均在隔离 MySQL 中恢复 12 个字段，核对金额、行数、哈希及任务引用。中断后用同一命令续跑。备份位于 `/mnt/data-disk/fb-page-current-20261123/history`，完成凭证是同级的 `history-ready.json`。核对后运行 `python3 -B recovery.py remove`，仅移除已冻结归档的旧表。磁盘统计和对应的读取时刻在 `history-dropped.json`。

## 回滚

- 候选读端未接流量时，原表继续服务。需要取消整个迁移时，执行 `python3 -B recovery.py rollback`。已切流量的兼容读端会由入口交接处理。
- 正式表切换后，执行同一个 `python3 -B recovery.py rollback`。脚本先从已验证画像补齐原格式表的最新版本，以只读端逐页核对，再在画像刷新空档原子切回。任务回执、分配账本和已打开页面的缓存仍由原业务管理。
- 历史表已经移除时，该命令从归档 DDL 恢复原结构，把当前最新成功画像装入恢复表后切回。需要补充某个历史版本时，用 `python3 -B recovery.py restore-version --version <完整版本号>`。隔离恢复和往返改名凭证位于 `qa/rollback-rehearsed.json`、`qa/postarchive-rollback.json`。
- 正在处理的同版写入由刷新端的 `building.json`、冻结差异和 `pending.json` 恢复。写入已提交而回执中断时，重试使用原版本从只读端核对。对照任务失败可从对应发布的 writer 目录运行 `python3 -B -m features.fb_page_current.mirror`，保留原本的自然任务号和冻结数据。

历史表分配量、数据库内可复用空间和云盘物理容量分别记录。共享表空间所在实例的其他业务可能同时读写，云盘容量只按实际可观测值报告。
