# SA 代码评审

2026-11-04，独立只读审查恢复模块、CLI、native 调度、中央准入和部署 helper。

## 发现

| 编号 | 级别 | 位置 | 事实与建议 | 状态 |
| --- | --- | --- | --- | --- |
| R1 | P2 | `features/fb_auto_posts/skipped_recovery.py` 的恢复 UPDATE | 其它批次若已占用同 Page、同 planned_publish_at_utc，唯一索引会抛 IntegrityError，让两批全部回滚。已离线复现：额外 active task 占用恢复时点，恢复数 0。应事务内预查并将该项保留为 page_task_conflict，其余继续；勿吞无关完整性错误。 | 已修复并回归通过：事务内按项检测目标时点占用，冲突项 retained，其余继续 |
| R2 | P2 | `scripts/deploy_fb_skipped_recheck.py:switch` 排空条件 | FB threads<=3 假定额外线程是两个后台核验线程。若后台尚未启动、被 RST 断开的 HTTP handler 在继续本地工作，单纯线程数与无 TCP socket 不能证明排空。需要明确当前后台线程证据，或基于实际已接受请求计数；普通主线程+handler 不应被误认成 main+readback。 | 已修复：仅减去 py-spy 线程名与 wait→queue.get→_reconcile_worker 栈均匹配的空闲后台核验线程；缺少证据不放宽 |

## 修复后结论

两项发现均已修复。独立复跑 `python -m unittest tests.test_fb_skipped_recovery scripts.test_pipeline_fb_task_isolation -q`，27 项通过。目标时点冲突现在只保留冲突项，下一批正常项可继续；部署排空不再仅凭总线程数将普通处理线程当成后台空闲线程。主流程另已在生产通过 py-spy 核实当前两个空闲 readback 线程及纯未来 planner，现场证据由主流程保留。

## 已核查成立的边界

- 原 task/run ID、原 gpu_job_id 保留；未知素材、全部历史已发布素材、同事务前一恢复项均被排除。
- `untouched` 检查 native attempt、ledger、claim、operation、hold 和关键任务字段；同 operation 已恢复项不会延期或重新选材。
- 每日 0 次项及非目标原因保持原行；当前模板版本/配置、当前 Page 归属/语言和授权数复验。
- 恢复窗口贯穿准备领取、制作完成、发布领取和过期处理；普通任务空窗口行为不变。
- 中央只允许原 skipped metadata-only、generation0 无历史执行的 prepare 观测恢复。恢复须先部署中央代码，不能先改 native 后让旧 collector 覆盖 source_status。
- CLI 通过 `__new__` 读生产 store 避免旧 constructor 重建索引；仅预演副本运行新构造器。部署逐文件基线检查并在副本演练两个构造器。
- apply 使用 15 分钟内候选缓存；随后复验 Page，事务内重读占用。候选内容并非 apply 时重新全量查询，此与现有冻结计划路径一致。

## 验证证据

- 中央恢复与既有隔离：`python -m unittest scripts.test_pipeline_fb_task_isolation -q`，20 项通过。
- R1 通过 `RecoveryTests` 夹具额外插入 run318、Page1001、当前恢复时点的 planned task 复现；异常为 `UNIQUE constraint failed: fb_auto_task.page_id, fb_auto_task.planned_publish_at_utc`，两个目标 run 均 0 个恢复。
- 本评审未执行生产变更或平台 POST。上线验收还应保留 226 目标任务中央 history/intent/receipt 为零的实际审计，确认重新扫描后正常项进入同一个 prepare job。
