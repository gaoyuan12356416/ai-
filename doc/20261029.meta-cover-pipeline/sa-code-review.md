# SA 代码评审

日期：2026-10-29。基线 4c7cd30。结论：已审核当前实现，发现的 P2 调度问题已修复并通过独立回归；未发现尚未解决的 P1/P2。本地完整回归已由主代理完成，发布仍需服务器验证。

## 审查范围与结论

- cover.py / cover_pipeline.py：广告与清理各自循环，广告不等待清理周期；只有一个 Governor、一个进程锁和 legacy lease。cleanup 默认 4 线程。Runtime 长连接只在协调器使用，SQL/Graph由各lane创建和关闭。
- 启动恢复在双lane前完成一次；小时refresh与legacy handoff通过协作停止和双join排空后再reconcile/释放lease。真实执行失败优先于并发handoff异常，避免掩盖账本问题。
- cover_worker.run_once：按完成事件补位，活动任务计数维护全局/账户容量；来源快照调用线程拥有；有界buffer和短TTL，过期未提交任务最多重取一次然后暂缓，停止时排空已提交future。
- 来源上下文从SQL快照开始计算年龄，不以查询结束重置TTL。没有跨周期身份缓存，没有扩大TTL。
- cover_store.pending_deletions：同账户旧Creative仍有pending/in_progress/waiting_review/unknown任务时不提前大扫描；废扫描未提交queued不永久阻挡。新增复合索引支持过滤，既有Meta完整引用检查、共享预约与一次删除未移除。历史waiting的next_check_at没有复位。
- cover_daily / cover_performance：日志写入用进程内锁，lane指标分列，吞吐分母使用活跃区间并集；旧日志兼容，空闲周期排除。无双重累计并行耗时。
- BUG-001：Meta accepted后的旧绑定回读延迟仅隔离该任务，保留fence且不重发。真实已绑定后的参数校验依旧执行；后续只读确认后才进入清理。

## 发现与处理

P2（BUG-002）：初版调度在每次空位补入后立即补查询1条候选，即使还有大量fresh buffer，可能退化为单条SQL。已改为buffer耗尽或确有无法填满的执行空位才重新准备；错峰完成专项断言初批8、次批4，防止退化。

## 独立验证

在模块目录独立运行：

- test_cover_worker.py：35 项通过。
- test_cover_pipeline.py：8 项通过。
- test_cover_daily.py：19 项通过。
- test_cover_scheduler.py：7 项通过。
- git diff --check：通过。

这些结果是当前实际执行结果，不替代主代理完整模块回归与Linux/生产验证。
## 主代理完整本地回归

主代理实际运行 `python -m unittest discover -s tests`：728 项，36.047 秒，OK（1 项跳过）；compileall 和 git diff --check 通过。随后补入的低水位查询修复另经独立 scheduler 7 项测试通过。Linux 候选和生产部署尚未执行，当前结论仅为本地验证完成。