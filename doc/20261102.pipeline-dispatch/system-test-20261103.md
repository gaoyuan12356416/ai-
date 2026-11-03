# 2026-11-03 系统测试与业务验收矩阵

本记录覆盖本轮监控/API/前端/权限/统计系统测试，以及随后定位的 FB 原生控制锁和回执重复投递问题。2026-11-03 10:00–10:07 CST 的线上部分全部只读：没有修改权限、容量、任务、预约或锁，没有发布/重试平台请求。前一天证据未覆盖，新增证据保存于本次私有工作目录 `work/acceptance-20261103/`。以下带 Z 的时间为 UTC，其余为北京时间。

当前进度（2026-11-03 03:24:16Z 只读核对）：协调器 `5b274ff`、X/FB/TT 原生服务和两节点 `95fdaa4` 已恢复制作，CPU 4 条、GPU 2 条共 6 条真实 render（TT 5、X 1）。独立 YouTube 原生桥为 `c097f39`、loopback 18847，主 API 进程未变。TT 于 03:22:13Z 恢复，X / FB 分别于 03:23:06Z / 03:23:18Z enabled；YouTube 按用户手动暂停保持 off。原八条 outbox 均已 ACK；含新增预检回执，CPU 为 80/80 ACK、GPU 为 28/28 ACK，双方 pending=0。新 TT 制作完成与最终发布仍待自然回执，不能将正在编码记成已完成。

完整 pipeline 套件在 `c097f39` 为 426 项通过（85.987s），TT worker 91 项通过（4.786s），监控 33 项及本地浏览器通过；后续 recipe 白名单修复 `5b274ff` 另有 44 项相关测试通过，未声称重新跑过 427 项完整套件，也不把各组相加。

监控、权限和本地测试通过不等于完整业务验收通过。下文 10:00 的发布 0 / 制作完成 5 / 失败 96 是修复前历史快照，不代表当前总量。节点升级、原回执收敛和真实制作恢复已有下文证据，制作完成与新平台最终回执另行补证；不能用 API 200、worker 心跳或编码探针替代平台最终回执。

## 测试范围、方法与结果

| 范围 | 方法 / 断言 | 本轮结果 | 边界 |
| --- | --- | --- | --- |
| 线上汇总 | 内部可信只读查询对中央 SQLite mode=ro；唯一 publication 事实与 8 类状态分别核对 | 全部一致；summary/workers/tasks 200，79/8/22ms | 单次样本，不声称压力测试或 SLA |
| 服务器/进程 | 动态节点集合、45 秒内活跃 worker、当前 busy 与运行任务逐项核对 | CPU/GPU 4 个节点阶段全部 alive/busy 匹配 | 以采样时刻为准，不能据旧快照推断后续状态 |
| 采集健康 | 原始 source_health 与 DTO 状态/采集时间核对 | 11 个来源均 healthy，stale=false，无 warnings | 健康仅代表账本采集成功 |
| 普通用户 | 读取两名真实 active 用户的权限，内部可信 actor 查询自有任务 | 返回 10/0 行，越权均为 0；scope=mine，隐藏全局 workers/capacity | 不伪造外部登录；FB numeric owner 的映射隔离由真实 SQLite fixture 另测 |
| 未登录 API | 公网无认证 GET summary/workers/tasks | 全部 401/auth_required，Cache-Control=no-store | 不进行生产写路由探测 |
| 首页/静态 | 公网根、监控 HTML/JS、quick-nav HTTP 与摘要 | 全部 200，摘要与 11 月 2 日部署一致；根默认监控和业务 hash 判断存在 | 线上 CUA 此前中止，未恢复或绕过；完整浏览器交互只在本地进行 |
| 桌面/移动 | 本地真实 Chromium，1440×1100 / 390×844，所有非 loopback 请求拦截 | 页面无 JS 异常；移动页无整体横向溢出，导航开合成功，表格在容器内横向滚动 | 数据均明确为 fixture，不计业务样本 |
| 管理员/普通/未登录 UI | 管理员容量/登记、个人隐藏全局管理、未登录 gate | 全部通过；未登录页面不请求监控数据 API | 容量保存/登记仅写本地假服务器 |
| 动态第三节点 | UI 三节点、真实 SQLite enrollment 与离线 heartbeat | 第三节点显示正常，stage/phase/等待原因可见，过期心跳离线 | 没有生产登记第三节点 |
| 调整与排空 | target<busy、停止接单 payload、配置 version、登记凭据清理 | 显示自然排空；不影响在途；一次凭据不进 localStorage，关闭清除；名称 100 字符边界通过 | 仅 fixture，不改变线上 target |
| 延迟与异常 | stale 来源、HTTP503、已有成功数据、真实 15 秒刷新 | 延迟提示/状态点可见；刷新失败保留上次数据；可见个人页自动再次请求 | 无长期后台标签页耗电/压力测试 |
| 深链与既有模块 | 执行 index 实际根跳转决策，验证 quick-nav | `/`、`/index.html` 无 hash 跳监控；`#tasks`、`#job/123` 保留；SocialKit/视频编辑保留；退休模块不恢复 | 旧任务全业务页由各自模块验收 |
| 输入与数据保护 | HTML 注入字符串、javascript URL、actor 参数注入、API token、same-origin | 转义/拒绝均通过，不泄露 payload/token；写操作必须管理员与同源 | 无生产恶意写入 |
| 统计语义 | 33 项监控/阶段指标/边界真实 SQLite 测试 | 全部通过，12.328s | 详见下节；不与完整套件数量相加 |

本地命令：

```text
python -m unittest -v scripts.test_pipeline_monitor scripts.test_pipeline_phase_metrics scripts.test_pipeline_review_boundaries
node scripts/test_pipeline_monitor_ui.js
node scripts/test_pipeline_phase_ui.js
node --check static/pipeline-monitor.js
node --check static/quick-nav.js
node --check scripts/test_pipeline_monitor_ui.js
```

浏览器采用已安装的 Chromium 1169，并显式设置 `PIPELINE_PLAYWRIGHT_PACKAGE`、`PIPELINE_CHROMIUM_EXECUTABLE`、`PIPELINE_UI_OUTPUT`。首次运行只因本地 1217 executable 不存在而失败，改用实际已安装 binary 后原测试及新增边界全部通过。未下载浏览器，未调用线上浏览器。桌面/移动/worker 截图已检查，位于 `ui/desktop.png`、`ui/mobile.png`、`ui/workers.png`；它们均为测试数据。

本轮基线完整 pipeline 套件为 352 项通过，66.626s（`pipeline-baseline-tests.txt`）；最终候选 `c097f39` 完整套件为 426 项通过，85.987s，另有 TT worker 91 项通过，4.786s。精确 TT 交接审计工具共 10 项，覆盖完整 source row SHA、范围/新鲜节点证据/无执行证据校验与事务恢复。专项和完整套件有重复覆盖，不累加为总用例数。

## 统计与权限语义

- 发布成功仅按可核实的平台最终 publication 唯一 source 计数；制作完成、预约上传成功、重复 receipt 不增加已发布。视频已发但首评 unknown/failed 时，已发事实与流程异常并列，发布成功率不把首评失败当作未发失败。
- metadata_only、缺失完成时间、无有效平台 ID 不伪造成功或耗时。活跃积压/unknown/历史暂停跨窗口保留，完成/失败按所选窗口统计；当前今天窗口从北京时间零点开始。
- 平台提交、平台确认等待、实际上传/处理分开；无证据显示 null/未记录，FB/TT 拉取型发布的直接上传显示不适用。旧 hash-only checkpoint、错误代次、负时间、失败/unknown 回执不制造确认耗时。
- owner/tenant/module/channel 在汇总和分页前过滤，admin cache 不供个人复用；FB numeric owner 不绕过手动任务 tenant 隔离。分页不重、权限不足模块拒绝、API token 不替代 Cookie 权限。
- 快照触发器缺失/安装中断能够事务回滚并重建；阶段回执及重复 checkpoint 仅计一次。历史 backfill 显示未完整提示并在收敛后不双计。

## 10:00 修复前历史快照（非当前状态）

协调器运行 `668d0b2d0d1a2111823ae3e819b850d2ae27f2b6`，X/FB/TT/YouTube 四渠道 enabled。采集快照生成时间为 02:00:03.864Z，最老来源采集时间 01:59:52.671Z，约 11 秒延迟。

| 节点 / 阶段 | target / ceiling | 存活 / 忙碌 / 空闲 | 节点心跳年龄 |
| --- | --- | --- | --- |
| CPU 制作 | 4 / 4 | 4 / 2 / 2 | 13.379s |
| CPU 发布 | 2 / 4 | 2 / 1 / 1 | 13.379s |
| GPU 制作 | 2 / 2 | 2 / 0 / 2 | 17.164s |
| GPU 发布 | 2 / 4 | 2 / 0 / 2 | 17.164s |

总量：queued=193、preparing=2、ready=1、publishing=1、prepared=5、published=0、failed=96、unknown=181、backlog_paused=1456。failed 的今天分布为 X 制作 68/发布 5、FB 发布 6、TT 制作 16、YouTube 制作 1。unknown/历史积压包含历史记录，不能全部算成本轮新增。制作成功是否有真实编码另由业务矩阵判定，监控的 prepared 本身不等于渲染成功样本。

原生 holds 为 X queue88+auto679、FB31、TTauto653、YouTube3，均保持原暂停清单；没有因本轮测试恢复积压或清锁。

## FB 原生控制锁故障与修复

只读证据 `fb-native-diagnosis.json` 与 `business-full.json` 确认：

| 中央任务 | 原生事实 | 当前结论 |
| --- | --- | --- |
| 1755 / `fb:video:3e33693bd5aa401b96a74998b3740f17` | 原生 16:00:00Z 领取；平台凭据 intent/attempt 到 16:00:37Z 才落账，晚于节点 10 秒超时；无 checkpoint/post ID；20 分钟后原生 worker_interrupted | unknown 保留，不重发。原生 attempt 不是平台成功证明 |
| 1756–1758 | 不同 Page 的原生领取依次 16:00:08/17/26Z；各 gen1 有 binding、无 operation/attempt；中央租约过期后 gen2 not_claimable，原生后来 tick 为 unknown | 中央 failed 与原生 unknown 的收敛问题交协调器处理；不清原生锁 |
| 1759 / 1760 / 1762 | 原生仍 queued/ready，无 binding/operation/attempt | 分别被前三条同批次同 Page unknown 前驱挡住；不能绕过原保护发后续任务 |

`FBNativeBridge` 全局 RLock 将慢素材/权限预检与凭据读取串行化，使不同 Page 及其他来源迟到回执互相等待。窄修 `5c8f04d` 改为弱引用 source_key RLock：同一 source 的制作、发布、代次和迟到回执仍共用一把锁；不保留闲置历史锁，不改变 SQLite 原子领取、Page/跨 auto-video 防重、审批、冻结素材或现有预留。无新增非标准库依赖，仓库只读查询每次使用独立连接。

新 `test_pipeline_fb_source_locks.py` 使用真实原生 SQLite，验证慢预检期间另一 Page 的迟到成功 checkpoint/complete 可以收敛，同 source 并发只领取一次，同 Page 和跨 auto-video 仍被原生 guard 阻止，跨代同源互斥与弱引用释放。连同既有 FB 回执/重领/暂停边界共 15 项通过，4.116s；py_compile/diff-check 通过。该修复已随 `95fdaa4` 原生候选于本轮部署；修复和部署均不重置 failed/unknown/inflight。诊断当时正在制作的 `fb:video:da3d5890853b4188a62d9539836879b1` 未被中断，当前业务状态以后续原回执核对为准。

## 回执投递并发与崩溃恢复

原 worker 的共享 SQLite outbox 只有已确认标记，没有跨进程投递租约；多个进程可以每次循环同时重交同一条 pending 回执。原生控制阻塞时，这会把少量未确认回执放大为并发 HTTP 请求，不能靠清空 outbox 处理。

窄修 `6bca72b` 将 `ReceiptOutbox` 提取为独立模块，并增量迁移原表。每条回执通过 SQLite 事务领取 delivery lease，阻塞调用期间独立续租；进程崩溃后租约过期才能由另一进程接续。原 route、payload、receipt/checkpoint ID、claim 身份保持不变。失败按 15/30/60 秒退避，最多 60 秒，保存有界错误码；同任务代次的前序未 ACK 时后序不投递，其他任务可继续。单次 flush 最多处理 100 条。fresh inline 回执可优先投递，后台至少避让 15 秒；only 查询遇其他 owner、前序未确认或退避未到均返回未完成。仅明确 `accepted=true` 且未返回 `ok=false` 才写 ACK，空字典和异常响应不会丢弃事实。

本地新增 11 项测试包括真实 spawn 三进程争抢、强制结束投递进程后的租约回收、慢调用跨进程续租、过期 owner 不得 ACK/释放新 owner、旧表迁移、前序阻塞、后台避让、退避、正向 ACK 和敏感错误过滤。连同 worker 与失败诊断共 35 项通过，7.619s。独立审查确认租约与退避逻辑，并补充了严格正向 ACK 回归。所有测试 client 均不访问网络，不重做执行任务。

worker 接线与领取握手提交为 `2f9e60c`，已包含在本轮候选中。两节点已停止旧进程并升级 `95fdaa4`；stage 前后原 outbox 身份及内容未变（当时 CPU pending 7、GPU pending 1），随后八条原事实均取得合法 ACK，双方 pending=0。两条 late preflight 首次补交曾返回 SQLite `OperationalError`，重交原事实后成功，没有重新执行任务；现存证据不足以断言具体 SQLite 锁持有者或锁冲突原因。旧实现不识别新增 lease，不能与新实现混跑。不得删除原回执、清 unknown 或借补交重新执行发布。

## 本轮确认的 BUG 索引

现有 BUG-001 为历史暂停问题，本轮顺延新增独立记录。以下修复均已纳入最终回归。协调器、原生桥和节点均已部署，原回执已收敛；各 BUG 状态分别记录，不将组件上线等同于业务验收。

| 编号 | 已确认问题 | 修复提交 |
| --- | --- | --- |
| [BUG-002](bugs/BUG-002.md) | 慢 claim 超过 10 秒，获得任务身份前没有独立心跳 | `2f9e60c` |
| [BUG-003](bugs/BUG-003.md) | X 全局控制锁阻塞无关 source 回执 | `2793af2`，进程保护测试 `575b08a` |
| [BUG-004](bugs/BUG-004.md) | FB 全局控制锁跨 Page 串行化慢预检和回执 | `5c8f04d` |
| [BUG-005](bugs/BUG-005.md) | 多进程共享 outbox 无投递租约/退避 | `6bca72b` + `2f9e60c` |
| [BUG-006](bugs/BUG-006.md) | X random 成片被中央恢复为预检原片指纹；迟到原回执纠正元数据与 recipe 白名单 | `2793af2` + `c097f39` + `5b274ff` |
| [BUG-007](bugs/BUG-007.md) | TT 选材后 fingerprint 和 selection phase 不能按冻结身份重领 | `dede4bf` |
| [BUG-008](bugs/BUG-008.md) | 已证明的 TT 原生账号占用被中央记为永久失败 | `6045458` + `2f9e60c` |
| [BUG-009](bugs/BUG-009.md) | 缺少明确 accepted=true 的异常字典可导致假 ACK | `6bca72b` + `2f9e60c` |

共同 78 项、各专项和最终 426 项存在重复覆盖，不能简单相加为总用例数。平台业务回执仍需另记。BUG-006 已有中央 31592/31595 与节点原成片不一致的生产只读证据，但原生发布仍读取验证后的 random 最终结果，没有证据证明真实发错视频。

## 本轮已完成的部署与精确交接

- CPU 协调器为 `5b274ff5c328f228fcc95a9ebb443a7439823b98`，升级备份 `/mnt/data-disk/pipeline-dispatch/backup-5b274ff5c3`；独立 YouTube canonical bridge 为 `c097f39efde069b8e40d548a6cdab4337c0cff0c`、loopback 18847。主 API PID 2281641 未变；截图任务仍归 API 进程，不能因配置 `DRAMA_JOB_USE_WORKER=1` 就假定 API 可无损重启。
- X/FB/TT 原生候选为 `95fdaa4c4db0ed19a465cb126d539274e95ea918`。03:07:49Z 只读复核 4 个 overlay lane 共 50 个文件，全部匹配 manifest after SHA；5 个原生服务 active。TT auto 现网 publisher SHA `da713109c6630fea1df7bba314053ee9f1ad9b837207a93ebf9a8785719a6afb` 保持不变。原 timer/path 状态已恢复，本轮临时 NEW TCP 规则为 0。
- FB 第一次切换因唯一 metadata `plan-next` 连接未排空而停止在门禁阶段，没有停止原生服务或覆盖代码。失败门禁已精确移除。该请求涉及未来 due 867287 的只读候选扫描，02:59:54Z 自然回到 pending，run_id 仍空；03:05:59Z caller inactive、accepted connections 为 0。随后按严格无制作/发布/连接在途条件完成切换，实际未使用 metadata 例外，未 SIGTERM 此 planner，也未重置 due/lease。
- 两节点均已升级 `95fdaa4`，保留身份、state、私有环境与 CPU 专用 TT FFmpeg 路径。stage 期间原 active manifest 不动，激活使用同一份预检候选；CPU 5 / GPU 9 个 TT profile 完整预检、实际 X repair、严格 TT outro 30fps/AAC 与 COS 通过。原八条回执已全部 ACK、pending=0；升级后的正常制作/发布结果单独验收。
- 经独立计划、完整 row SHA 和双方节点新鲜 inactive 证据核对，TT exact 8 条无执行交接已由中央 failed 恢复 queued，generation 仍为 2：4544/4546/4549/4553/4554/4569/4570/4571（中央 31474/31476/31479/31483/31484/31625/31626/31627）。只改精确中央交接状态；原 caps/history/schedule、原生账本与文件 SHA 不变，没有执行制作或平台请求，没有推广到其他 failed/unknown。

TT 时间采用原始 UTC 字符串一次换算：前五条原生交接时间为 **11 月 2 日 21:33Z = 11 月 3 日 05:33 CST**，后三条为 **11 月 3 日 00:43Z = 08:43 CST**。先前把 PowerShell 已转本地时间再次加 8 小时得到的 13:33/16:43 不采用，也不据此计算等待时长。

BUG-006 的 `c097f39` 只在原历史 claim、原生最终成片及迟到回执 SHA/size/URL 全部吻合时纠正中央误存的 source artifact；不新建任务、不改变已完成代次、不重新制作/发布。后续 `5b274ff` 保留 `artifact_from` 白名单中的已验证 recipe，避免正确成片已入投影后配方被裁掉。03:19:58Z 对中央 31592/31595 精确纠正成功：最终 SHA 分别以 `ec9eb359` / `4883f412` 开头，各产生一条 `qualified_final_artifact_corrected` 审计事件；两条 completed/status 和 generation=2 不变，没有重做制作或发布。

证据包括 `coordinator-5b274ff-deploy.json`、`x-final-artifact-exact-correction.json`、`x-late-artifact-corrected.json`、`cpu-original-receipt-response.json`、`gpu-original-receipt-response.json`、`read_native_final.json`、`native-*-apply-*.jsonl`、`read_fb_scope.json`、`stage-cpu-main-95fdaa4c4db0.json`、`stage-hk-gpu-95fdaa4c4db0-attempt2.json`、`tt-handoff-applied-84c6cb6758f64a58ab1da69b7c44aefe.json`。TT 正式计划 SHA 为 `659041517629132431e4c8459ce668912b3c97dc2711ebb1e223a139a50b5a31`，恢复前中央备份与原生未修改结果已保留。

## 03:24Z 恢复制作与保护边界核对

03:22:13Z 恢复 TT，随后 03:23:06Z 开启 X、03:23:18Z 开启 FB。03:24:16Z 双节点运行时读回与 03:24:35Z TT 业务核对证明 CPU 4 条、GPU 2 条真实 render 在执行，合计 TT 5 条、X 1 条；不是仅靠 worker 的 busy 标志判断。任务独立 15 秒心跳已持续跨越原 120 秒失租故障窗口。两台发布池各 2 个进程存活，此刻 idle/account_busy 等待成片或同账号资源，不代表发布进程缺失，也不证明新 TT 已发出。

原八条回执全部已 ACK；新增预检事实继续正常入账，CPU outbox 80/80 ACK，GPU 28/28 ACK，双方 pending=0。没有清空 payload 或重新执行旧平台请求来消除积压。

03:24:16Z 线上监控 summary/workers/tasks 均 HTTP200，耗时 129/12/28ms；8 类状态、唯一发布事实和动态节点与只读账本全部一致。两名普通用户分别可见 13/0 行，越权均为 0。此前冻结保留的 17 条精确旧任务全部 unchanged；1,454 条原生 hold 的 source key 集合不变，被领取数为 0。监控 backlog_paused 投影总量 1,456 与原生 hold 数为不同集合，分别按各自账本核对，不混用总数。

证据：`pipeline-monitor-after-repair.json`、`cpu-restored-runtime.json`、`gpu-restored-runtime.json`、`retained-after-repair.json`。TT 交接已有独立只读、冻结身份和无执行证明；运维辅助 scratch 副本后缀探针的覆盖修正单独记录，不据此重启生产或重复执行交接。

## 仍需完成的业务验收

本轮监控、前端及系统回归通过，协调器/原生桥/节点已上线，原八条回执收敛，双机 6 条真实制作已恢复；制作完成与新 TT 发布最终回执仍需核对，不能称四渠道新一轮完整业务验收通过。YouTube 此前已有 4 条经验证的真实最终发布事实，本轮用户已手动暂停 YouTube，保持 off，不为等齐六条封面而延迟恢复其他制作，也不因文档验收重新开启。未知回执仍需核对，失败不自动重试；精确八条 TT 无执行交接是另有审计授权的窄恢复，不推广到其他历史任务。

关键证据：`pipeline-monitor-acceptance.json`、`monitor-public-auth.json`、`monitor-python.log`、`monitor-browser.log`、`ui/`、`business-full.json`、`fb-native-diagnosis.json`。证据原件留在私有工作目录；文档不保存 session、token、私有环境或签名 URL。


## 14:11 排队统计文案澄清（待部署）

总览、渠道表及状态筛选将 queued 的“待领取”统一改为“排队中”，并明确包含待选材、未到预约时间及账号等待，不等于当前可领取数。原 queued 状态值、计数、筛选请求、权限及调度规则不变；不添加未经验证的可领取数量。静态 JS 缓存版本更新为 `20261103queue1`。

依据：`views._summary` 按原状态汇总，增量计数只排除 `metadata_only`，尚未执行的 Xauto `control_only` 父计划也在 queued 中。14:11:36 CST 只读快照的 198 条 Xauto queued 父项均为原生 pending，全部被同账号前项挡住；197 条对应 65 个 retry_wait/reconcile 前项，另 1 条对应 preparing 前项。这里只确认阻塞关系，前项是否应收敛仍由独立账本核对，不能据此宣称全部属于正常等待。证据为私有工作目录 `work/backlog-20261103/read_x_planner_waiting.json`。

验证：现有本地浏览器契约检查总览仍显示原 fixture 数字 22、筛选仍使用 `queued`、说明文案可见；生产统计与任务未修改。
