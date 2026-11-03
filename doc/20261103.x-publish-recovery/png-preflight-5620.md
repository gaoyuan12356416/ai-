# q5620 已知输入不兼容结算

范围固定为 q5620 / log5543 / prepare job32153 / material7676040 / account63 / schedule1039 / generation17。不是恢复发布工具，不重新选材、不关闭随机模板、不把图片转换为视频。

2026-11-03 13:11Z 只读确认 `ads_custom_source.url` 本身就是 PNG（type=1、duration=0），不是误取封面字段。其 URL 指纹同时匹配原生队列、17 代冻结 descriptor 和全部预检回执。GPU 实际探测为 image/png、1080×1920、2815842 bytes；17 次都是下载/探测和源文件存储，没有启动随机视频渲染，也没有任何 X 平台 operation。

原生 `preflight` 在写入任何 artifact/checkpoint 前以 `x_random_requires_video` 拒绝图片+随机视频模板。coordinator 把该明确错误压成 `coordinator_operation_failed`；outbox 中前序 checkpoint 无法确认，阻塞同代后序 fail；worker 已退出执行并停止租约续期，导致 17 次到期重领。`started_at` 保留第一代时间，不能当成第17代的实际开始时间。

`scripts/x_png_5620_incident.json` 冻结原队列/日志、17代双账本、hold事件、parent1039身份以及34条原包的原始字节/脱敏内容/claim三重指纹。CPU节点该任务没有任何回执；GPU有17条预检checkpoint和17条 unknown=false fail，全部未ACK。

`scripts/settle_x_png_preflight_5620.py` 使用以下顺序：

1. 两节点回执证据均须120秒内，当前双库完全匹配固定证据；无平台operation、checkpoint、成功receipt/delivery、artifact、render、资源占用、未知或Post。中央必须仍为held queued/gen17，租约已过期。
2. 两库在线备份、0600文件、quick_check；中央持久journal保存原始行与证明，撤销当前claim_hash，保留generation17、17代历史、全部原生claim及现有自定义hold。旧claim原hash仍在不可变历史/审计内。
3. 原生原queue/log原地标记failed，代码`x_random_requires_video`。attempt0/unknown0不赋新值，素材/模板/正文及其它字段保持。与不可变审计同事务调用实际`XPostStore._sync_run`汇总parent1039所有当前兄弟任务；不覆盖旧快照计数。
4. 中央标记failed并保留原hold，journal完成。中途退出使用原plan重跑即可；完成后重跑不写入、不重新备份。

仅failed+hold不能拒绝迟到回执，因为当前core允许过期claim提交。先撤销当前claim_hash是必需fence。不会写`pipeline_delivery`、`pipeline_receipt`或`canonical_receipt`，不会访问节点outbox进行写入，34个pending包原样保留。其后被claim_conflict拒绝不等于ACK；本次不声称清理了outbox。

运行示例（发布源先走GitHub，root审阅后才apply）：

```sh
umask 077
mkdir -m 700 /private/5620-settlement
python -B scripts/settle_x_png_preflight_5620.py node-evidence --node-id cpu-main --outbox /mnt/data-disk/pipeline-node-cpu/pipeline-dispatch/receipts.sqlite3 --output /private/5620-settlement/cpu.json
# GPU用同一已提交源的node_evidence只读函数；不改GPU runtime或服务。
python -B scripts/settle_x_png_preflight_5620.py plan --central /mnt/data-disk/pipeline-dispatch/dispatch.sqlite3 --native /var/lib/x-post-automation/accounts.sqlite3 --evidence /private/5620-settlement/cpu.json /private/5620-settlement/gpu.json --output /private/5620-settlement/plan.json
python -B scripts/settle_x_png_preflight_5620.py apply --plan /private/5620-settlement/plan.json --evidence /private/5620-settlement/cpu.json /private/5620-settlement/gpu.json --backup-dir /private/5620-settlement/backup --actor codex-x-recovery-20261103 --output /private/5620-settlement/result.json
```

原计划可用新的节点证据执行；若超过120秒则重新采集两节点证据。journal已fenced后的续跑仍要求fresh节点证明；已完整完成的幂等检查不要求fresh。命令公开输出仅任务ID和操作结果，原计划/证据/备份须留在私有维护目录。

测试：`python -m unittest scripts.test_x_png_preflight_5620 -q`。覆盖真实SQLite约束、claim验证、双库各中断边界、原包及历史不变、幂等、未知/新操作/租约漂移阻断、审计防修改/删除/REPLACE、实时兄弟任务聚合及禁止网络调用。生产冻结证据也已在本地按固定manifest复核通过。

后续共享组件修复另行评审：节点在提交预检前识别图片与随机视频模板不兼容；coordinator保留明确AdapterError；为已证明无平台副作用的永久预检拒绝设计可审计终结协议，仍须遵守各代权属和outbox顺序。不能通过跳过未知平台回执或伪造ACK解决。当前工具不修改共享coordinator/节点协议，也不重启或打断其它上传。
