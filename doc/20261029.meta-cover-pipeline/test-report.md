# QA 验证报告

日期：2026-10-29（北京时间）。状态：本地/Linux验证、部署、有界canary及冻结常驻多周期验收通过。

## 版本与测试

运行代码：`23b3d606c12ea15bc2c5104cf041e45648e1ef8e`。[PR #24](https://github.com/gaoyuan12356416/ai-/pull/24)。

| 验证 | 实际结果 |
|---|---|
| 独立 worker / pipeline / daily / scheduler 专项 | 35 / 8 / 19 / 7 项通过，共69项 |
| 本地完整模块回归（首次业务实现） | 728项，36.047秒，OK，1项跳过 |
| Linux首次业务release55d706f完整回归 | 728项通过，无跳过 |
| Linux包含窄安装器的完整回归 | 733项通过 |
| 最终23b3d60安装器专项（新增固定快照修复） | 本地/Linux均8项通过 |
| compileall、git diff --check | 通过 |

最终备份修复仅修改安装辅助代码，业务执行代码未变化。完整733项与随后8项安装器验证按各自实际范围记录，不宣称另跑过736项。

事件型专项证明清理阻塞时广告继续、慢广告不形成整波屏障、SQL与网络重叠、停止/让锁排空、共享Governor唯一、旧绑定读回不重复POST、过期来源不写和并行指标不双计。

## 缺陷与修复

- BUG-001：Meta accepted后立即GET仍返回旧绑定，被误判为全局参数异常。现仅将该任务置unknown和保留fence，随后只读核实，不重发POST。
- BUG-002：滑动调度初版可能错峰每完成一条就查一条来源。现先消费fresh buffer，只有buffer用尽或确有无法填充的执行空位才准备新批。
- BUG-003：活跃writer使逐页备份反复重启。现固定源只读事务，progress检查15秒预算，finally释放源事务，目标完整性校验在释放后执行。
- 同Creative未完成任务过滤避免过早大扫描与小时暂缓；废扫描未提交行不永久阻挡，历史waiting检查时间不复位。

## 来源准备只读对照

同一64任务/64账户：两批32合计12.337秒，单批64为4.597秒；64条均有效，准备结果完全相等。此为一次固定样本对照，不是持续吞吐保证。

## 部署与有界canary

窄apply成功，主API PID `2111068` 未变化；API Store未替换，共享账本未恢复、未迁移。备份与回滚见 deploy.md。

临时单次验收unit：`meta-cover-pipeline-canary-23b3d60.service`。19:44:22结束，Result=success、Exit=0、inactive。本次广告与清理lane各一个周期：

| 指标 | 广告lane | 清理lane |
|---|---|---|
| 周期开始 | 19:38:42.672 | 19:38:42.668 |
| 实际执行开始（凭据准备后） | 19:38:50.178 | 19:38:46.788 |
| 周期结束 | 19:40:20.335 | 19:44:22.610 |
| 含周期准备耗时 | 97.663秒 | 339.942秒 |
| 结果 | 256任务：169成功、78等待审核、9失败 | 61个旧Creative确认删除 |

9条失败均为Meta主页未发布：subcode1443017共7条、2490604共2条。没有复位或自动重试。

来源准备256任务使用7次查询批次：64×1、32×6；累计40.009秒，与广告网络处理重叠，过期刷新0次。共享Governor观测最大in-flight32，配置maximum32、per_account2、rps24，两个lane没有各自翻倍预算。

旧停机task9021通过只读完整核实，单独从unknown转为成功，不计入169条新成功。原5条unknown降为4；没有新unknown、没有新的全局暂停、没有重复task write_intent。对canary新成功task9028、9027、9568、9068、8946再次Graph GET核实，绑定与全部保护参数通过。

该单次canary证明两个lane重叠执行、广告周期早于清理周期结束；常驻后续周期证据见下方追加。平均2.33小时历史队列年龄不能与此97.663秒周期直接比较。

## 常驻启动

19:45:53启动 `meta-cover-worker.service`，PID `2215811`，当时为active/running；scan/notify timer均enabled、active/waiting；旧cleanup timer保持disabled。启动事实与下方最终冻结常驻观测共同构成验收证据。

## 证据

服务器目录：`/mnt/data-disk/meta-violation-material-cleanup/diagnostics/pipeline-23b3d606c12e/`。

- pause-readonly-review.json
- pause-before-resume.json
- before-canary.json
- canary-result.json
- prepared.json

保留原pause证据、未知/失败回执、一次删除账本。没有通过复位历史状态制造验收样本。
## 常驻中间观测（19:48:16）

PID2215811为active/running，NRestarts=0，无pause。首个常驻广告周期19:45:59.700至19:47:35.446，95.746秒处理256任务：192成功、55待审核、8失败、1暂缓pending；来源查询仍为64×1、32×6。

首个常驻清理周期尚未结束时，第二个广告周期已进入执行（32条in_progress），且新删除回执持续增加。这直接验证后续广告周期无需等待清理周期结束。此处仅记录中间检查点，不将当时累计账本数量当作最终冻结验收结果。
## 最终冻结常驻验收

冻结时刻：**2026-10-29 19:49:36.198+08:00**，最后事件id23334。证据为 `diagnostics/pipeline-23b3d606c12e/continuous-verification.json`。release为23b3d606c12ea15bc2c5104cf041e45648e1ef8e，Git工作区干净。

worker enabled、active/running，PID2215811、NRestarts=0、pause=false；scan/notify timer enabled、active；主API PID2111068 active且未重启。旧清理timer保持disabled。

| 常驻广告周期 | 开始 | 结束 | 耗时 | 结果 |
|---|---|---|---:|---|
| 第1周期 | 19:45:59.700357 | 19:47:35.445864 | 95.746秒 | 192成功、55待审核、8失败、1pending |
| 第2周期 | 19:47:35.493889 | 19:49:07.622062 | 92.128秒 | 208成功、40待审核、8失败 |

两个周期均处理256次任务、来源准备256条，仅7个查询批次（64×1、32×6）；批间48ms。第1周期1条pending原因为cover_context_expired，保留队列供后续刷新，没有扩大TTL或用过期上下文写入。两个周期完成时清理仍进行且删除回执增加，冻结时第3个广告周期已开始，直接证明广告循环不等待清理周期。

截至冻结时刻：

| 账本状态 | 数量 |
|---|---:|
| 广告succeeded | 4663 |
| 广告pending | 2108 |
| 广告in_progress | 32 |
| 广告waiting_review | 196 |
| 广告failed | 252 |
| 广告unknown | 4 |
| 广告blocked | 1 |
| 广告obsolete | 3787 |
| 旧Creative deleted_verified | 3823 |
| 旧Creative pending | 516 |
| 旧Creative waiting_references | 7 |

相对19:38:34基线：成功增加604，其中1条为原task9021只读恢复、603条为新成功；旧Creative确认删除增加148；pending从2967降至2108。新增write_intent为607条，含冻结时仍在途的操作；重复task write_intent为0，不把所有已提交请求当作已成功。

这是冻结点事实，不是全量队列清空声明。周期计数是处理次数，可包含暂缓后的后续处理；不得把它们与去重成功任务数混淆。本次没有重测充分的全量入队等待分布，因此不声称原2.32小时平均等待已降低到某个值。