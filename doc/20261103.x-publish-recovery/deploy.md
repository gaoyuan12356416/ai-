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

## 第二轮修复的部署与后续精确恢复

提交 `179596a9699393cd377c0e06a0bec94e171f14cd` 已推送 GitHub。内容为 X 源下载只读网络失败重试、归因解析只读 5xx/网络失败重试、短剧候选排除所有历史队列已占用的精确 episode_key；Post/Repost 不增加重试。

GPU 于 11:18:51 UTC、CPU 于 11:41:57 UTC 在各自原任务自然完成后启动 179596a。CPU 原 8 个发布全部成功，未强杀上传或渲染；GPU 原 5 个 render 全部 completed。X 于 11:49:14 UTC 切换到 179596a 候选，实际 PID427278；Auto PID1604845、coordinator PID1304161、原 schedule scanner PID2555019 保持。X 与原五个 timer 于 11:56:17 UTC 恢复，global config version101，其他平台保持启用。

X 健康检查实际响应是 HTTP200 纯文本 `ok\n`。11:47 的第一次切换因操作者按 JSON 解析而误报失败并回滚；11:49 修正检查后切换成功。使用已独立验证的 `--job-mode=ignore-dependencies`，短暂暂停并原 PID 恢复 scanner，避免 Requires 依赖传播重启。所有临时连接规则已清理。审计 `runtime-switch-179596a-corrected.json`。

只增量部署 schedule runner 43b2aa9 与 12:19:44 UTC 的 a212e3e 四文件读预检修复；当前 scanner PID1478253 未重启。新增只读网络/数据库明确暂态错误分类，最多两次相同范围读取；不会给 Post 增加重试。只有重新确认原冻结身份、零 plan/零 queue 的批次才允许延期到下一 tick。

补充恢复：5406/5445 的媒体 APPEND 失败、5449/5454 的源下载失败、5451 的零平台写入归因失败，均保留原身份和媒体。旧 5280 精确恢复后又收到 initialize HTTP403 账号锁定，已停止相关账号未开始的任务；不清零 attempt。5524 经原 worker 代码、PID、双 ledger、两节点原始 ACK 独立证明仅到 APPEND，完整保留 unknown 历史后授权原下一代。其余 unknown 继续逐项核对，不批量清标记。

Auto 另有 3 条原成功回执校正，累计 7 条。62 条已审计已知 pre-Post 失败恢复只读 reconcile 跟踪，另对 Auto2395/5524 保留 unknown 标志恢复只读观察；不会调用重新发布或重新选材。最新 12:30:55 UTC 原生今日成功 89 条，成功状态差异为 0；这只是该时刻快照，不表示积压清零。

原 1039 已生成 38 条队列。1029/1034 第三次精确恢复审计绑定此前所有原批次授权，零队列且原身份不变；1031 必须等 1029 实际生成队列再处理，防止相同历史 episode 的并发选择。1036 仍由原 scanner 持续扫描，不杀进程。

## 回滚边界

代码回滚仅切回本次记录的运行目录，并重新做健康检查；不恢复整库，不回滚平台成功记录，不把 unknown/已发帖状态转回待发。恢复授权和审计表留存，已执行下一 generation 的任务不能清除 attempt 或再用旧 plan。Node 回滚同样须自然排空，禁止强杀媒体上传或 FFmpeg。

## 第三轮候选与精确数据恢复（进行中快照）

候选 commit f3392f330544333c9d35952eac7311fe46a24d1e 位于 `/mnt/data-disk/pipeline-dispatch-runtime/x-publish-recovery-f3392f3/candidates/x`，以当前实际运行树为基线，仅差 service.py、x_native.py 与新 recovery_1389.py 三文件。候选实际导入上述模块的20项专项测试通过。保留脱敏错误详情使既有账号锁定识别有效；1389专用原子准入仍保留旧released预留，须等候选实际启用后才可授权。

12:39前固定9条media-only逐条完成双库审计、原next-generation授权，各次计划与双节点新证据/备份保存在media-only-{queue}-{timestamp}目录。9条Auto只读观察在nine-auto-observe-1241完成，计划c2ec94ec2654bb5f7b317f0fcdc3d77d2a408965c9a6a43e2927f001b331c62e；Auto历史unknown保留直至真实成功。恢复队列累计97（其中旧5280的Auto原计划实际为10/30，不可错误归入11/2；它的新尝试仍是初始化403失败、没有Post）。

两个API再次返回锁定的账号49/71已通过正常publish-approval接口暂时取消发布批准，保存two-locked-accounts-publish-approval.json；精确中央留置的任务不影响其它账号。13条Auto未建队列旧失败，其原素材已被后续8个不同任务确认占用，不能直接重开或复活released预留，等待用户选择是否用新素材补齐。

2658/2674原任务只有首次账号快照失败事件，语言从未冻结且执行/素材字段全空。12:56以标准账号verify重新取得安全DTO，按原task/template/date/account补齐首次快照并回pending，无新建task/发帖/清零attempt。审计auto-missing-two-125601，计划4f1b93f0cf41c35943461fc145355cd6c1e3cd96f2bfd66bf0f0e7cc614312ce。

固定1029入口12:47:35创建19条正常队列（5633–5651），没有legacy发布调用，保留其它账号的原安全阻塞。1031在12:45:17由另一维护actor codex-p2p-ou_62fed22c-229-recovery恢复，本线程没有重复rearm；原scanner1478253于12:49:29自然退出，普通timer已恢复，新scanner2771735继续1034/1036。1031下一次正常预检返回drama_shortage，未重置。

第三轮只暂停X新领取，config102，其它平台保持enabled；原15条在途自然完成，不停止节点。短切评估拒绝以历史1.8s启动时间当作可靠上界：API客户端30s、service启动90s/停机6h，无法保证请求无失败。没有对8810做额外DROP/REJECT试验。候选尚未实际激活，后续需补最终切换和恢复领取证据。

## 第三轮实际切换及后续状态（13:45 UTC）

f339候选未激活，由19504a5ea47f33469cb2ab49bc1c9aea127a677d替代。冻结随机视频模板的正常素材选择现跳过PNG等图片候选，其它默认选择行为不变。13:20:44只增量更新runner/selector；scanner2771735保留，继续其已加载版本直至自然完成。1036在13:31:33建成42条原队列5667–5708。

原8个CPU发布均取得成功回执，最后一条q5579于13:16:24成功；6个CPU准备自然完成。GPU q5620实际是PNG预检拒绝后的反复租约领取，并非长时间渲染，已先精确hold。13:21全部X running=0后，才在13:28:45–13:28:46进行X侧切换：停止原5timer、确认无Auto claim/请求连接，短暂保护8810并暂停原scanner，切current到195并健康确认后移除规则、继续原scanner。原Auto PID2057294和coordinator PID1304161保持，未重启节点/主API。新X PID3607439、NRestarts=0。审计runtime-switch-19504a5.json确认规则清理及scanner继续。

13:33:40恢复X channel及原5timer，config103只改X启用状态，其它平台配置保留；phase3-x-resumed.json留证。13:42快照已有101条今日成功回执，按原计划日11/2为66条、11/3为35条；8条发布、9条准备，Auto成功显示差异为0。积压仍在推进，不宣称已清空。

1389精确准入没有执行：持有Auto/manual原锁并重新核验后，发现另一次正常manual1652已于12:49:44为同账号43/素材5188422创建q5657，helper在queue_exists处拒绝，零写入且无grant表。原1389/1426 released历史保持，不能再创建相同素材帖子。原1388/1399/1400实际队列分别5573/5576/5577，均排队，不能把邻近队列5574/5575/5583的成功误配给这些任务。

13:41:21对q5620完成精确PNG失败结算：原17代claim和34个pending包全部保留，先持久撤销当前claim_hash以拒绝迟到包，再将原生queue/log和central标记x_random_requires_video。attempt0/unknown0保留，未调用平台。父1039按实时兄弟任务汇总。审计png-5620-settlement-20261103T134121Z，plan6e556a92409f3af02c35e9a44b6f00fa1d67eaaadfefc5361360b0f3931f1f41。未清理或伪造outbox ACK。

1031在临时oneshot x-recovery-1031-once-20261103内只读等待1029的19个原队列取得已知终态；Restart=no，最多24小时，30秒观察一次。ready后只准入一次原1031，让正常runner继续；任何漂移/异常/未知/无候选立即留证退出，不循环恢复。13:43仍等待19个依赖，没有业务写入。工具来自已推送63fc341，当前运行代码proof固定195。路径1031-dependency-once-20261103T134200Z。启动SSH因Type=oneshot等待完成而超时，但已通过独立systemctl和持续events日志核验实际程序PID963052正常等待，未重复启动。

13:44:52按268a1b94已提交工具给1034追加第四次精确审计，plan5fadc0fb9a41e50dea2478ec51b62117b09014f4436a99e09c8391a79bf77941。已独立验证13:05:17容量409在历史create_schedule_plan的首个事务之前，原计划尝试时间完整保留，前三审计不改；原89账号/snapshot4444保持。1036的42个队列先提交后才恢复原1034为claimed，交正常runner，不自行选材/发帖。证据1034-fourth-20261103T134500Z。

13:49补充验证：1034由正常timer启动的scanner1338429处理（13:45:10启动，heartbeat13:48:54），仍在预检、未跨plan fence。8810/18833/18846的/health均HTTP200。1389替代关系也已明确：batch327键x229-recovery-original-manual-1389→personal373→manual1652→q5657，与原personal361的正文、账号/素材snapshot指纹完全相同；原1389全行未改，未创建恢复grant。该替代由既有维护流程建立，本线程仅核对并阻止再次建单。
