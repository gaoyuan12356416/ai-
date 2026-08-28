# 部署与回滚

## 首次发布代码（后续补充见下节）

- CPU：43.166.187.96。
- GitHub分支：`codex/x-pool-blockers-20260828`。
- 生产提交：`814e168a48bbacae00384c0950576c5b253e21eb`。
- 当前release：`/mnt/data-disk/x-post-automation/releases/814e168a48bbacae00384c0950576c5b253e21eb`；`/opt/x-post-automation/current`已原子切换。
- 旧release：`/mnt/data-disk/x-post-automation/releases/18d9cfb68ee330db633b769112f0a50e38bc3e7c`。
- 备份：`/mnt/data-disk/x-post-automation/backups/20260828-pool-blockers-155312`。包含在线SQLite、切换前新快照、受控env、Token目录及主API共享代码。Token不入Git/报告，不自动回灌已轮换Token。

## 部署记录

1. 本地832项回归通过（Windows跳过2项）；新增配置解析的11项聚焦回归通过；最终生产提交在Linux通过832/832（38.593s）。
2. online backup quick_check=ok/FK=0；备份副本初始化后726 queue/726 log/171 relay/130 run/55 drama pool/841 material pool数量不变；识别账号8 locked、19 unknown，其余17个素材账号/16个短剧账号可用。
3. 主API共享service.py较旧，未发现其独立新行为。保留主APIapp/selector/OAuth，精确同步service.py和两个新helper，全部有备份及hash。
4. 外部操作曾于15:47:47暂停、15:59:58恢复三个timer。第一次部署门禁因状态变化拒绝，零代码/env变更。重查无inflight、代码/env/保护账本无漂移后，记录当前active状态，执行本次短暂维护暂停。
5. 16:11:04/05分别重启主API与Sidecar。Sidecar PID1777039工作目录指向目标release。配置独立开启deferred drama GPU repair：profile v5、repair timeout3600、schedule内部请求timeout7200；专用修复Token仅从服务器原受控配置复制。
6. 首次主API健康探测错误使用其不存在的/health得到404；未把404当成功。随后用真实/api/auth/status验证200及X admin接口401鉴权门禁，Sidecar/GPU /health均200。
7. 16:13:44健康及SQLite检查通过，保护queue/log/relay 533/719/726逐字段与备份一致，三个timer恢复部署前active状态。线上只读预检已返回精确409中文原因。

## 历史短剧恢复（独立阶段，未等同于代码上线）

- 清单：`deploy/recovery/x-post-drama-run-348-media.json`（635—647，13条）和`x-post-drama-run-350-media.json`（667—669，3条）。
- 先对备份副本完整媒体validate-only和apply演练；全部证明通过后，暂停原定时器、确认无在途X写、再次在线备份，再执行live validate-only/apply。
- 保留原target/relay/素材ID/内容/episode/绑定及queue/log主键；修复URL单独留原URL与不可变审计。仅重置已证明零尝试/无ID/无unknown的媒体失败。
- 不重建363、不清q533/719/726，不用真实测试帖验证。数据恢复只表示回到原队列待执行；真实发帖须以后续自然timer和ledger核对。
- 当前媒体校验/恢复进度见test-report.md；不得把本节计划步骤当成已经完成。

## 下载完整性与重查分类补充（2026-08-28 17:45）

- GitHub下载补丁170e3b1325b71a72fcd6de913982ce92bb77fa40在CPU通过836/836、香港GPU通过138/138。GPU于17:34切到`/data/x-post-media-repair/releases/170e3b1325b71a72fcd6de913982ce92bb77fa40`，PID1780296；运行时与状态目录保留迁移后的/data，不触碰US worker/tunnel masks。
- 17:35 CPU/MainAPI/CPU18820健康通过，原三个timer恢复active。GPU17:40对q642真实下载收全83,863,962字节，SHA与CPU相同，仅1次GET，没有X写入。
- 17:42显式幂等start香港tunnel，随后CPU18820再次200/profile v5。17:41快照GPU active repair jobs=0，CPU无media_uploading/post_creating/repost_creating。此前下载诊断已结束，不做压力测试。
- 素材池重查分类补丁dad987b430cfa4001b23c6517c2fdaa5c70f5f2a再次通过Linux836/836。17:45 CPU已切换该release，Sidecar PID1832335，main service SHA为87c42536dac4a1a9b8eb906c973ef056f7b5dfaaddc7b391d327448184fde7cf；GPU维持170e，不因CPU候选分类改变而重启GPU。
- CPU补充证据：原备份目录的`download-fix-deployment-result.json`、`recheck-fix-deployment-result.json`、`linux-x-tests-download-fix.log`、`linux-x-tests-recheck-fix.log`及两份切换前在线SQLite。q533/719/726逐字段不变，16条历史仍failed、未rearm。
- GPU补充证据：`/data/x-post-media-repair/backups/20260828-pool-blockers-download/deployment-result.json`、`gpu-tests-download-fix.log`、`backup-meta.json`；诊断证据在`/data/x-post-media-repair/diagnostics/20260828-pool-blockers/q642-postfix-download.json`。

### 历史媒体证明复用

- 首次run348整体CPU报告为failed，不能当作成功证明；7条旧GPU ready文件已通过SSH验证root属主、权限和精确资源范围，再固定bundle SHA为78e5830d4d6303b906d5402ee87285e54031846d1e5200e7198221d8f4fca08e。
- 原冻结行快照frozen-inputs.json SHA为a5343590f87b5f0890e2e5e3ade68404c41b976e520926949f9652914468491f，包含16 queue/16 log/11 relay/16 pool及受保护三条记录。
- 使用事故专用helper严格核对GPU完整ready版本、请求、job、COS路径，再真实下载并探测CPU输出；逐项成功后才写私有checkpoint。其余9条仍走既有预检/修复；不中断原绑定、不换源、不混入失败证据。
- checkpoint有效期4小时，固定索引SHA、工具commit、源身份和完整CPU证明；缺失、过期、冲突立即停止。prepare入口只允许备份副本/apply=False。后续copy/live apply必须复用全部完整证明、实时源身份查询和原store事务guards，并持整个阶段的process_lock；每个run独立checkpoint，第二批失败不重复apply第一批。

## 恢复工具版本固定（2026-08-28 17:59）

- CPU通过GitHub fetch取得e300542887fb89314bef145b752c3ad8aa6c5c9c并归档，Linux856/856（38.484s）通过。发布service.py SHA与dad987b相同，只有恢复工具/测试/文档变化。
- 全程原process_lock、暂停并恢复三个原timer、无在途写、在线备份与冻结行比对后，只重启Sidecar，PID1840134。主API未再重启，HK维持170e3/PID1780296。
- 健康8810/8787 auth/18820均200、X未鉴权401；16条失败队列及q533/719/726仍逐字段不变。证据为recovery-ops-stage-result.json、recovery-ops-deployment-result.json、linux-x-tests-recovery-ops.log、accounts.before-recovery-ops.sqlite3。
- 后续apply命令为deploy/recovery/x_post_incident_apply_20260828.py，独立GitHub归档目录取其精确提交名，报告operator_commit/operator_sha256；它明确导入CPU e300542的helper与store，checkpoint/audit继续绑定e300542。该步骤不切换current、不重启CPU/HK服务。
- 两个run全prepare完成后固定最终index SHA。先copy phase，逐字段核对所有业务行和16条完整audit，再live phase。两个run先全部validate-only，随后逐批apply并持久化进度；一旦进入apply，失败时不自动恢复timer或重复整批。

## 回滚

1. 停止三个timer，等待schedule/claim/manual服务退出，并查publish log无media_uploading/post_creating/repost_creating；不得强杀在途写。
2. 原子切回旧release，例如先建`/opt/x-post-automation/current.rollback`指向上述旧release，再用`mv -Tf`替换current。
3. 恢复备份中的`main-api-before-service.py`到主API的features/x_posts/service.py；两个新helper可保留为未使用文件，避免删除后续工作。
4. 恢复备份中x-post-automation.env和x-post-schedule.env至/etc，保留原属主权限；不要恢复Token或整库快照。
5. 窄重启drama-material-api.service、x-post-automation.service；复查/api/auth/status、X鉴权、Sidecar /health、commit/hash与SQLite。
6. 若历史恢复已apply或自然发布开始，不删除恢复审计、不回写旧URL、不整库回滚。保持timer暂停并核对真实ledger，再决定后续恢复。
7. 若仅回滚下载补充：CPU保留各阶段before-image与相邻旧release；香港只在`/data/x-post-media-repair/releases`内切回fba8ff6并重启worker，随后显式start香港tunnel并复查CPU18820。不得恢复旧/var/lib或/opt运行路径，不得解mask/启动US备用节点。
