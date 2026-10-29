# 部署与回滚

日期：2026-10-29。状态：部署、有界canary及冻结常驻多周期验收通过。

## 实际版本与目标

- 主机：43.166.187.96。
- 运行目录：/mnt/data-disk/meta-violation-material-cleanup。
- 当前业务/代码release：`23b3d606c12ea15bc2c5104cf041e45648e1ef8e`。
- 旧版本：`4c7cd30878ad46dd2908839a60dc09642ab2eda9`。
- [PR #24](https://github.com/gaoyuan12356416/ai-/pull/24)。
- 一致备份：`/mnt/data-disk/meta-violation-material-cleanup/backups/cover-worker-20261029T113650284944Z-23b3d606c12e`，含plan.json、hashes.json、配置/units、三份活动SQLite审计备份。

## 窄部署路径

在指定GitHub release的模块目录运行：

```bash
python3 deploy/install_cover_worker.py prepare <release>
python3 deploy/install_cover_worker.py apply <backup>
```

专用prepare固定scope=cover-worker-only，不带--active-ledgers-only。要求封面scanner/worker/notifier空闲、旧清理配置/服务/timer停用；candidate/current封面Store精确同hash，API的11个封面预约/一次删除/事务/claim方法及3个helper的AST兼容，prepare后和apply再次核对。无关API查询/审批方法和其他活跃删除任务不要求停止。原通用安装器及其全局任务gate保持原样。

只切换代码链接及worker/通知units，不替换API、不执行共享schema迁移、不恢复共享账本。实际apply成功，主API PID2111068未变化。

## 备份一致性与锁预算

共享账本约1.9GB；持续writer可能让无固定源快照的逐页备份反复重启。23b3d60在只读源连接BEGIN并SELECT固定快照，以pages=256、busy sleep=0.05复制。progress回调检查15秒预算；成功/异常均finally rollback源事务，目标integrity_check在释放后进行。

DELETE journal下其他writer可能短暂等待，预算低于既有API30秒busy_timeout。预算按progress回调检查，不是操作系统硬截止。三份SQLite各自一致，不声称跨库同一瞬时快照。备份仅作审计，回滚不用于覆盖最新业务状态。详见BUG-003。

## 暂停核实与验收

原2026-10-29 14:15:28停机样本task9021只读核实为Meta旧绑定读回延迟；新Creative及完整参数验证通过。原pause已保存证据后才恢复。该task通过只读核实消除unknown，没有重新POST。

临时canary unit meta-cover-pipeline-canary-23b3d60.service于19:44:22以Result=success、Exit=0结束：广告lane97.663秒处理256任务（169成功、78待审核、9主页未发布失败）；清理lane339.942秒确认删除61个旧Creative。没有新unknown、全局暂停或重复task write_intent；5个新成功task再次Graph只读验证通过。详见test-report.md。

19:45:53启动常驻meta-cover-worker.service，PID2215811，当时active/running；scan/notify timer为enabled、active/waiting；旧meta-violation-material-cleanup.timer仍disabled。最终冻结验收见下方。

## 精确回滚程序

先停止新调度并排空worker，再等待已运行的scanner/notifier自然结束；它们必须inactive/failed后才运行安装器：

```bash
systemctl stop meta-cover-scan.timer meta-cover-notify.timer
systemctl stop meta-cover-worker.service
systemctl show meta-cover-scan.service meta-cover-notify.service -p ActiveState
# 上述两个oneshot均已结束后：
python3 /mnt/data-disk/meta-violation-material-cleanup/releases/23b3d606c12ea15bc2c5104cf041e45648e1ef8e/server-monitors/meta-violation-material-cleanup/deploy/install_cover_worker.py rollback /mnt/data-disk/meta-violation-material-cleanup/backups/cover-worker-20261029T113650284944Z-23b3d606c12e
```

安装器恢复旧代码链接、worker及通知units，保留最新账本及所有成功/失败/未知回执，保持worker停止；switch_report会enable --now通知timer，但不会自动恢复扫描timer。不恢复SQLite、不恢复已消费写入意图、不重启主API。检查配置、pause和未决结果后，才决定是否重新启动旧worker并恢复scanner调度。回退会恢复旧版同步清理的性能行为；旧Creative删除不可逆，不能通过回退代码恢复ID。

## 审计证据

`/mnt/data-disk/meta-violation-material-cleanup/diagnostics/pipeline-23b3d606c12e/`：pause-readonly-review.json、pause-before-resume.json、before-canary.json、canary-result.json、prepared.json。
## 最终常驻验收

冻结时刻2026-10-29 19:49:36.198+08:00，事件id23334。current为23b3d606c12ea15bc2c5104cf041e45648e1ef8e且Git干净。worker enabled、active/running，PID2215811、NRestarts0、无pause；scan/notify timer enabled、active；主API PID2111068保持active且未重启。

连续广告周期95.746秒和92.128秒，间隔48ms，清理期间第二、第三广告周期继续推进。相对19:38:34基线，新成功603条，另1条原unknown只读恢复；旧Creative确认删除增加148；pending2967降至2108。无新unknown及重复task写意图。完整冻结状态、样本范围与性能解释见test-report.md。

最终证据：`/mnt/data-disk/meta-violation-material-cleanup/diagnostics/pipeline-23b3d606c12e/continuous-verification.json`。本次接受的是执行流程优化及安全恢复，不宣称队列已全量清空或历史2.32小时等待分布已完成新基线测量。