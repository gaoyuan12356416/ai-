# X 发布恢复部署记录

本次恢复范围为北京时间 2026-11-02、2026-11-03 的已有任务。先核实平台回执；未知结果不直接重发。所有恢复保留原任务、账号、素材、文本、历史尝试和审计身份。

## 已执行的第一轮恢复

- 4 个自动任务按原唯一成功 Post 回执校正状态：2312/2354/2436/2566。不发送平台请求。
- 70 个确认未到 Post 的失败任务恢复原队列：50 个发布阶段、20 个准备阶段。其中原计划日 11 月 2 日 48 个、11 月 3 日 22 个。准备阶段保留 attempt=0，发布阶段保留原 attempt 并授权精确下一 generation。
- 6 个确认零平台写入的归因查询失败队列 5399/5407/5486/5544/5550/5552 在只读查询恢复后重新就绪，原媒体不重制。
- 原手动运行 1388/1389/1390/1399/1400/1401 和原定时运行 1029/1031/1034/1036/1039 恢复原身份。恢复触发不等于已完成发布；后续预检失败另行核实，不能重复套用第一次恢复。
- 账号临时锁定发生于 media initialize 阶段，尚未发帖。用户明确确认 @Vani4ka94、@AroajsQ 均已解锁后，恢复原队列 5413/5511/5514/5531/5448。保留原 attempt=1 和已确认失败历史，逐一授权下一代执行。
- 14 个未知结果继续隔离。5452 曾被其他维护流程重置，已依据原历史和回执恢复中央 unknown 隔离；不覆盖其原始事件。

远程私有审计根目录 `/mnt/data-disk/x-publish-recovery-20261103`，每次写操作均保存计划、行级摘要和数据库备份。主要证据目录包括 `confirmed-receipts-backup-2`、`safe-prepost-70-20261103T104744Z`、`attribution-exact-104958`、`user-unlocked-original-four-110422`、`hold-5452-backup`。

## 运行时与并发变更保护

采用独立分支 `codex/x-publish-recovery-20261103`；提交先推送 GitHub，服务器只拉取精确提交。运行时基于当前生产树叠加已审核文件，逐文件核对哈希与基线，不覆盖凭证或其他线程修改。GPU 原并发变更和色彩转换修复 306b1673af29b1903987f23b906467ce09b871f0 已合并保留。

第一轮 X sidecar、Auto 与 coordinator 于 2026-11-03 18:44:52 北京时间完成切换与健康核对。X 和 coordinator 的实际根目录为 `/mnt/data-disk/pipeline-dispatch-runtime/x-publish-recovery-a97fc23/candidates/{x,coordinator}`。只暂停 X 新领取和原五个触发器，已领取任务自然完成；未暂停 FB、TT、YouTube channel，也未回写旧全量配置。原五个 X timer 于 18:51:59 恢复。

首次切换的本地 8810 NEW 连接拦截曾误阻断 ExecStartPost 健康检查，启动失败后按实际 PID、运行目录及 symlink 完成核对，清理全部临时规则。随后采用允许服务用户自身健康检查的精确规则完成切换。审计分别保存在 `runtime-switch-a97fc23.json` 与 `runtime-switch-a97fc23-corrected.json`，不将第一次尝试记录为成功。

修复所属运行时为 X sidecar；主 API 的相关请求委托到 sidecar，没有覆盖 `/root/drama_material_service` 或重启主 API。

## 第二轮修复的部署状态

提交 `179596a9699393cd377c0e06a0bec94e171f14cd` 已推送 GitHub。内容为 X 源下载只读网络失败重试、归因解析只读 5xx/网络失败重试、短剧候选排除所有历史队列已占用的精确 episode_key；Post/Repost 不增加重试。

截至 2026-11-03 19:08 北京时间，X 增量候选已准备，只有 `features/x_posts/service.py`、`features/pipeline_dispatch/x_native.py` 两个文件发生变化；现存 schedule runner 1212323 的修复保留。X 新领取再度暂停，原运行中的上传和制作继续。CPU、GPU 节点通过安装验证及 13 个上传/下载专项测试后进入自然排空，不能把候选 manifest 已更新误报为新进程已运行。

第二轮完成后的实际 PID、哈希、恢复计划和验收数据应追加至此文。1029/1031 的第二次精确恢复必须在新 service.py 部署哈希匹配后执行，详见 `drama-episode-recovery.md`。

## 回滚边界

代码回滚仅切回本次记录的运行目录，并重新做健康检查；不恢复整库，不回滚平台成功记录，不把 unknown/已发帖状态转回待发。恢复授权和审计表留存，已执行下一 generation 的任务不能清除 attempt 或再用旧 plan。Node 回滚同样须自然排空，禁止强杀媒体上传或 FFmpeg。
