# 部署与回滚

当前部署进度（2026-11-03，恢复制作核对中）：协调器运行 `5b274ff`，独立 YouTube canonical bridge 运行 `c097f39`（loopback 18847），X/FB/TT 原生服务和两节点均为 `95fdaa4`。主 API PID 2281641 保持不变；原八条 outbox 已全部 ACK，pending=0。YouTube 按用户手动暂停保持 off，TT 维护恢复及 X/FB 恢复制作正在收尾，制作/发布 readback 待补，不能声称本轮四渠道业务全部验收。下列 11 月 2 日记录为历史基线，不代表今天的组件和业务终态；本轮最终业务验收见 [system-test-20261103.md](system-test-20261103.md)。

**11 月 2 日历史末态：**协调器、X 原生服务和 CPU 节点运行 668d0b2，GPU 节点运行 CA 修复版 b349295。四渠道于 2026-11-02 15:10:25Z 恢复接单，15:25:59Z 仍全部启用；旧触发器按原状态恢复，本次临时端口/防火墙规则全部清理。新流程已有 CPU/GPU 真实制作、Facebook 和 X 最终发布回执；TT unknown 保留，GPU TLS 修复后未重试原任务，TT/YouTube 完整业务验收待正常任务。以下组件版本来自实际部署核对，不能从本地 HEAD 推断其他线上组件。

## 11 月 3 日窄升级记录

`c097f39` 完整 pipeline 426 项通过（85.987s），TT worker 91 项通过（4.786s）。随后 recipe 白名单修复 `5b274ff` 通过 44 项相关测试；未重复跑完整套件，不写成 427 项 full pass。协调器最终部署 `5b274ff5c328f228fcc95a9ebb443a7439823b98`，备份为 `/mnt/data-disk/pipeline-dispatch/backup-5b274ff5c3`。独立 YouTube 桥保持 `c097f39efde069b8e40d548a6cdab4337c0cff0c`，避免重启仍承载截图线程的主 API。

原生候选目录 `/mnt/data-disk/pipeline-dispatch-runtime/native-95fdaa4c4d`，GitHub 提交 `95fdaa4c4db0ed19a465cb126d539274e95ea918`。X/FB/TT/TT auto 4 个 overlay lane 分别 11/15/12/12 个文件全部 after SHA 匹配，5 个原生服务 active。TT auto publisher 完整 SHA 为 `da713109c6630fea1df7bba314053ee9f1ad9b837207a93ebf9a8785719a6afb`，保留原生产代码。备份位于候选的 `backups/`；操作审计为 cutover 目录中的 `native-x-95fdaa4c4d.json`、`native-fb-95fdaa4c4d-resume.json`、`native-tt-95fdaa4c4d.json`。

原 timer/path 恢复各自原状态，03:07:49Z 检查本轮临时 NEW TCP 规则为 0。FB 首次等待连接超时未覆盖代码，门禁已清理；唯一只读 metadata 查询自行完成，后续以 accepted=0、原生制作/发布=0 严格排空切换。未使用 metadata handler 例外，未中断该查询，未重置原 future due。

TT 8 条已证明无执行的 selection 交接只作为部署排空的精确例外，原生 preparing 状态/冻结内容未动。另经专用审核计划与备份完成精确中央 failed→queued，generation 保持 2，caps/history/schedule 和原生 SHA 保持；这不是解除历史 hold、unknown 或恢复其他失败任务。

CPU/GPU 已升级同一份 `95fdaa4` 预检候选。原节点身份、state、原八条回执身份及内容保留；CPU 专用 TT binary 不变。CPU 5 / GPU 9 TT profiles、实际 X repair、strict outro 30fps/AAC、COS 预检通过。八条原回执已全部 ACK，双方 pending=0；两条 late preflight 曾返回 SQLite OperationalError，重交原事实成功，没有重执行，尚无证据确定具体锁原因。中央 31592/31595 只纠正 artifact 到已验证最终 SHA `ec9eb359…` / `4883f412…`，各一条审计事件，status/gen2 不变。

YouTube 此前 4 条真实发布事实已经核实，本轮继续保持用户手动暂停，不等待六条封面补齐。恢复后的 TT 制作/发布及 X/FB 正常任务证据后续另记，不以服务 active 或回执 ACK 替代新业务最终回执。

本轮回退仍须先关闭新领取并排空，在相应服务停止后用上述 manifest 的 `rollback` 恢复代码/配置。不得将部署备份数据库覆盖当前账本，不清理旧 outbox、publication、unknown、素材预留或历史暂停。

## 11 月 2 日运行边界与版本记录（历史基线）

CPU 43.166.187.96，香港 GPU 43.154.250.89。CPU 数据目录 `/mnt/data-disk/pipeline-dispatch`，GPU 数据目录 `/data/pipeline-dispatch`。协调器监听 `127.0.0.1:18846`，远程节点经受保护连接访问；不开放公网，旧 8846 属于其他服务。

| 组件 | 已确认事实 | 最终运行 SHA / 路径 |
| --- | --- | --- |
| CPU 协调器 | 原子领取、投影/暂停账本、监控 API 已部署 | 668d0b2d0d1a2111823ae3e819b850d2ae27f2b6 |
| TT / TT auto 原生服务 | 窄更新 tt_native.py，现网 publisher 保留 | ba2ee7ff5ead8e38a90fcbf8ea9113880f554d8f |
| X 原生服务 | 从 d6858fe 窄覆盖升级；保留严格 waiting_relay 暂停，修复重复身份查询与受控媒体 401 恢复 | 668d0b2d0d1a2111823ae3e819b850d2ae27f2b6 |
| FB 原生服务 | 本轮保持 | 6829f94a220b008747a32ff8b725f9d60f2e85d8 |
| 主 API / YouTube | 监控 gateway、权限及原审核流程保留，本轮保持 | 8d0e25ec59715a86994d986d15f7119f48f73ab3 |
| 静态默认首页 | 根无业务 hash 进入监控，业务深链与 SocialKit 保留 | 15:11:17Z HTTP SHA256 已核对，见下表 |
| hk-gpu 节点 | 9 个 TT profile、实际 X repair 与严格 outro 30fps/AAC 通过；CA 修复后 15:25:10Z 激活，PID 3960674；实际进程 TLS 验证通过，identity 保留 | b349295c4045ded1c99816ed5f122dfbe7447bb4 |
| cpu-main 节点 | 5 个 TT profile 完整 verify、实际 X repair 与严格 outro 30fps/AAC 通过；15:08:13Z 激活，PID 963393 | 668d0b2d0d1a2111823ae3e819b850d2ae27f2b6 |
| TT legacy 只读核对 | 仅既有 publish_id，排除活动租约及 pipeline-owned 任务 | `/etc/systemd/system/` 中专用 unit/timer；执行 `/opt/tt-auto-post/current/scripts/tt_auto_post_reconcile.py` |

| HTTP 静态资源 | 15:11:17Z 响应 SHA256 |
| --- | --- |
| `/` | `4ef6284b79abc495a27dcfd97f00f59c64f59ea96942efe958183cc69c85e121` |
| `/pipeline-monitor.html` | `9fafacf32b1e420c6f1c32660a292a94b0ca539051571413d2d5268a8f4853ee` |
| `/pipeline-monitor.js` | `5e5349b269cd8cf441f3d9d0ff0f81cd83688f1f7b3aa59d33759aacabffb7c7` |
| `/quick-nav.js` | `69f8f3bc001187b3c8a0118b59470576a9cbb866e85d3a62dba05c186f73205f` |

TT legacy 只读核对 timer 使用 OnBootSec=30s、OnUnitInactiveSec=2min、AccuracySec=5s。service 的 User=tt-post，WorkingDirectory=`/opt/tt-auto-post/current`，ExecStart=`/usr/bin/python3 /opt/tt-auto-post/current/scripts/tt_auto_post_reconcile.py`，TimeoutStartSec=45min；已通过 systemctl cat 核实，不记录私有环境内容。

前置 `social-publish-isolation-20261102` 已核对并保留。X 在本次工作期间又上线素材池保存及查询索引修复；部署基线逐文件固定到已拉取的 GitHub 提交，不能用统一旧基线覆盖。生产 TT publisher/runner 比最初仓库基线新，保留其冻结模板路由、并行轮询、Code 同步及已有功能；新只读核对放在独立 pipeline 模块，不迁移或整文件覆盖旧生产实现。

本轮 TT 原生升级只改 tt_native.py，旧 publisher 文件摘要保持 `da7131…a6afb`（完整值在部署私有 manifest）。CPU TT 使用独立 root-owned binary，原服务 binary 保留。668d0b2 完整 pipeline 347 项通过，54.480 秒（23:05:29 CST），TT worker 91 项通过，3.714 秒，X 独立复验 47 项通过；预检通过不等于该渠道已有最终平台业务回执。

b349295 为安装器/doctor 的 CA 增量修复，相关 41 项与独立 45 项通过。GPU 运行目录为 `/opt/pipeline-dispatch/releases/b349295c4045ded1c99816ed5f122dfbe7447bb4`；CPU 运行目录为 `/mnt/data-disk/root-storage-20260915/rootfs/opt/pipeline-dispatch/releases/668d0b2d0d1a2111823ae3e819b850d2ae27f2b6`。两机 checkout 均 clean，CPU PID 963393 在 GPU 修复期间未重启。

旧 GPU 1a83de8 的 doctor 曾返回成功，但未检出实际 outro 为 25fps。该结果已撤销为完整规格证据；新版真实输出强制 30fps，并检查输出帧率和 AAC。CPU X repair 原先将 NVENC tune=hq 带入 libx264，已移除该参数，预检改为执行实际完整 repair 命令。两节点 668d 的完整安装器验证覆盖这些真实失败路径，不沿用旧 doctor 的结论。

## 切换步骤与不变约束

1. 本地完成回归后提交并推送 GitHub，服务器拉取精确 SHA。抓取非秘密运行源码摘要，以审计 manifest 逐文件演练增量补丁。per-file patch base 只有与已拉取提交的真实文件 SHA/已验证规范化 SHA 匹配才可使用；冲突即停止，禁止整文件静默 fallback。
2. 先安装不接单的协调器、节点、原生桥和监控，原数据库在线备份，迁移仅增量。共享 GPU 服务、视频 worker、统一回执 writer 和其他业务 unit 不因本次切换重启。
3. 按渠道冻结 cutoff、原任务暂停清单、正在执行的精确 item 及其资源锁。停止旧领取，等待已接受的单条任务和必要后续发布；祖先豁免只从 exact child 关系传递，不能扩大到整批。hold 保持素材预留、去重、原队列状态和回执。
4. 原生服务旧进程并非全部支持 SIGTERM 优雅退出。延长 TimeoutStopSec 不能代替排空：停止旧调用器后核对账本，必要时只临时拒绝对应 loopback 端口的 NEW TCP；ESTABLISHED 请求继续完成。确认已接受请求、连接和账本在途均为空后，才停止受影响服务。X 还须检查内置分享线程独立账本。
5. guard 版监听端口及进程环境核实后，精确移除本次临时连接规则，保留其他防火墙规则与健康检查。旧执行 RPC 交接排队，不下载/制作/发平台；metadata planner 与只读核对仍可工作。canonical bridge 使用原内部鉴权和独立开关。
6. 对节点做最终用户身份下的工具、profile、资产及真实短编码/COS 预检，再恢复已批准的新领取。只使用正常到期任务进行业务验收，不为填满渠道矩阵额外创建平台测试帖子。

制作领取时间与发布时间分别保留：FB 原计划器已生成的 planned/queued 随机任务可立即制作，仍遵守重试时间，不扩大原计划器预建范围。TT 自动任务使用协调器私有 JSON 的显式 `preparation_policy.tt_auto_prepare_ahead_seconds`；本次生产值 `TT_AUTO_POST_PREPARE_AHEAD_SECONDS=14400`，两处保持一致，原生桥继续将 executor 配置传给原领取校验。发布阶段不提前，`cutover_due_at` 始终为原发布时间。未领取 generation0 制作任务可刷新资格时间；已领取、暂停和 unknown 任务不重置。

## 11 月 2 日自然排空与业务核对（历史基线）

21:13:15 通过 admin/stop-claims 暂停 X/FB/TT 新领取，剩余 3 条制作自然排空。21:39:35 只读快照为全部渠道关闭、中央 executing=0，两节点未 ACK 回执均为 0。此前新流程已完成 CPU 制作 3 条、GPU 制作 1 条及每节点各 1 条 Facebook 最终发布；这不是当前开关终态，后续以新快照为准。

CPU/GPU 双向读取对方成片均校验完整 SHA 和大小。GPU→CPU 的约 1 GB 文件用时 851.020 秒，是一次已记录的网络瓶颈样本，不能将此时间算为制作或平台发布耗时。最终监控按下载、制作、COS 传输、平台提交、确认等待等可验证阶段展示；缺失时序保留空值，不伪造完成时间。

四渠道 15:10:25Z 恢复新领取。此前 TT 4530/4533/4528/4537 在 creator_info 即失败，原始审计确认无 task_publish_started、无 pipeline_tt_operation、无平台发布 POST；4538 为素材资格校验失败。4540/4541 的旧 GPU 制作与 manifest 保留，但任务 failed，不计新节点成功，不自动重试或重做。这些失败不会因修复上线被重置。

新 TT 4534/4535 在 GPU 各 init 一次后进入 unknown/tt_upstream_unavailable，无 publish_id 或成功 checkpoint，未重试。GPU urllib 默认 CA=0，OpenSSL 默认路径位于服务用户不可读的旧 root/miniconda 目录；初步 certifi 子进程覆盖后 CA=120、未认证官方 HEAD 返回 404。GPU 单独优雅停接后完成 b349295 安装器/doctor 修复，于 15:25:10Z 激活；15:25:22Z 使用实际进程环境复验，CA=120、X/TT urllib 官方 HEAD 均 404，TLS 验证开启。CPU 668d 继续 active，TT unknown 及锁保持，未因 TLS 修复重发原 init；HEAD 不是发布回执。

TT 修复需以实际生产 frozen route 为准：reserve/complete 均拦截 route.gpu_client；source_key 锁保持同源各阶段与迟到回执互斥，不阻塞其他账号。新任务按实际 profile 冻结 CPU/GPU backend；source_direct 保留实际接单节点 backend。两机必须先验证各 profile 的工具与资源，不能仅声明能力。

CPU 权威 TT 服务应配置批准的 `PIPELINE_TT_APPROVED_OUTRO_SHA256` 和 `PIPELINE_TT_APPROVED_FONT_SHA256`。首次 direct_outro 的 auto/intake/direct_test 接单前，节点 `asset:tt-outro:<sha>`、`asset:tt-font:<sha>` 必须与批准值匹配；缺失或错配不创建原生 claim。已冻结描述器续代和完成继续沿原 caps/请求，不随配置变化换配方。批准哈希来自当前实测资源，不证明旧 4540/4541 当时未冻结的 font SHA。

X 5381 的账号 19 因旧桥接逐媒体操作强制身份查询，在约 4 分钟内发生 74 次成功核验后限流。668d 保留领取时强核验和原权限门禁，操作时仅按原刷新规则获取令牌；媒体 401 的一次重试绑定原操作完整指纹及同一 claim，不用于发帖/转发 POST，不重跑任务。5380 原 x_upstream_error 缺乏当时平台 HTTP 状态/正文，不能补判为授权或媒体规格问题。两条 X unknown 锁继续保留，失败与未知任务不会自动重试。

新增诊断只保存脱敏且可核实的失败信息；诊断自身失败时舍弃可选字段，原 code/unknown 回执仍先持久化，避免诊断故障丢失回执。broker 限流 cooldown 仅适用于当前冻结且经中央校验的资源，不改变 unknown 或重发资格。CPU↔GPU 的 X 源成片交接已真实发生，跨机传输本身不计为平台发布成功。

其后的 X 5385/5387 已由 CPU 取得最终平台回执，分别于 15:19:36Z、15:20:05Z published，Post ID 为 2117275437915160647、2117275561353519477；两条均 generation1/attempt1，完整 checkpoints、原生与中央账本一致。它们作为新流程 X 发布成功证据，不改前述失败或 unknown 状态。

15:25:46Z 最终只读监控验收：summary/workers/tasks 为 200，耗时 73/7/18ms；唯一原生发布事实、8 类状态、动态节点、权限及 hold 与账本一致。普通用户无越权（15:11:17Z 样本分别返回 81/0 行），根默认页/业务 hash 保护及公开未登录 API 401 均通过。原生 holds 保持 X 767、FB 31、TT 653、YouTube 3，未随渠道启用恢复旧积压。

15:25:59Z 中央共 9 个新鲜心跳 worker：CPU 制作 4/发布 2，GPU 制作 1/发布 2。四渠道启用，active_jobs=0；CPU outbox pending=0/acknowledged=28，GPU pending=0/acknowledged=8。当前 target 为 CPU 4/2、GPU 1/2，当前空闲原因是原账号锁或无合格任务，不是缺少 worker。

## 容量、未来节点与回滚

旧负载排空且新领取关闭时，曾通过带审计的容量配置恢复初始 target：CPU 制作 2/发布 2，GPU 制作 1/发布 2；初始 ceiling 为 4/4、2/4。它们是启动配置，不是永久硬编码。实际 target 随真实资源与完成样本自适应，节点无需重启即按目标进程数接单；提高 ceiling 不等于已经提高 target，更不能补写样本。低内存缩容或持续高 CPU 缩容只减少新接单，不杀在途。

新增 Linux CPU/GPU 服务器按 node-enrollment 文档登记唯一 node_id、私有凭据及实测能力，中央和页面动态读取节点；无需改两台服务器白名单。每个阶段 target/ceiling 可配置，停止接单保留当前任务。第三节点仅有离线协议/UI 覆盖，未实际部署。

回滚先关闭新领取，等待在途和 outbox 排空，再退对应组件代码/配置；保留当前账本、token、成片、hold、publication 和迟到回执。unknown 继续隔离核对，不重新初始化平台发布。未来 YouTube 预约保留原 video_id/时间，只做必要读回和已授权接续；同频道历史未知锁不移除。CPU 私有 cutover 目录保存计划、暂停清单、manifest 和操作审计，回退使用对应 manifest，不能恢复旧 SQLite 备份覆盖新回执。

最终运行证据为私有审计目录的 `pipeline-hk-gpu-b349-live-tls.json`、`pipeline-hk-gpu-b349-final-audit.json`、`pipeline-cpu-main-post-ca-final-audit.json` 和 `pipeline-monitor-acceptance.json`。TT 4534/4535 保持 unknown，失败/unknown 均未重试。当前没有合格待接 TT 新任务，TT/YouTube 新流程最终发布仍待正常任务验收；不把 service active、TLS 或编码探针当业务完成。不将私有环境凭据或签名 URL 入库。
