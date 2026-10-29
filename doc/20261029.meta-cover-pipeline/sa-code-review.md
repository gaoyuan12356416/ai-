# SA 代码与验收评审

日期：2026-10-29。基线4c7cd30；最终运行代码23b3d606c12ea15bc2c5104cf041e45648e1ef8e。结论：发现的问题均已修复，未遗留阻塞性P1/P2；本地/Linux、有界canary和冻结常驻观测通过。

## 代码审查

- 广告与清理独立持续循环；只有一个Governor、一个进程锁和legacy lease。清理默认4线程；Runtime仅协调器使用，SQL/Graph由各lane创建、关闭。
- 启动恢复在双lane前完成一次。小时refresh、legacy handoff和停止均先协作排空、双join，再核实/释放lease。真实执行错误优先于并发handoff异常。
- 滑动调度按完成事件补位，维护全局/账户容量。来源SQL留在调用线程，最多64条有界准备；fresh buffer未耗尽且执行槽满时不逐条补SQL。TTL从查询开始计算，过期未提交任务最多重取一次后暂缓，不放宽30秒条件。
- 同账户同旧Creative有未完成任务时，清理不提前大扫描；废扫描未提交行不永久阻挡。完整Meta引用检查、共享预约、删前TTL及一次删除账本仍保留。
- 共享Governor保持总32、账户2、24req/s；两个lane不会将限额翻倍。
- 并发性能日志用写锁，lane分列，总吞吐分母为活跃区间并集。历史记录兼容，不双计重叠耗时。
- accepted后旧绑定GET延迟只隔离该任务unknown，保留fence且不重发。真正参数漂移仍执行原验证；只读确认成功后才进入清理。
- 窄部署只切封面代码/units，current/candidate共享Store精确同hash，API相关14项AST契约在prepare前后和apply复验。原通用活跃任务gate未改；不替换API、不执行共享schema迁移、不覆盖账本。
- 最终备份固定源只读事务，15秒progress预算，finally rollback后才目标integrity_check。DELETE journal writer可能短暂等待；预算不是操作系统硬截止。

## 发现与关闭

- BUG-001：旧绑定读回延迟误触发全局停机，已窄化为unknown隔离。原task9021最终只读确认成功，没有重发POST。
- BUG-002：滑动调度逐条补来源查询，已改消费buffer后批量准备，错峰测试通过。
- BUG-003：活跃writer导致备份重启，已固定源快照并有界释放，8项安装器验证通过，实际备份成功。

## 验证与接受范围

独立运行worker35、pipeline8、daily19、scheduler7和窄安装器初版5项通过；主代理完整Linux733项通过，最终23b安装器8项本地/Linux通过，编译及diff检查通过。具体版本和范围见test-report.md，不宣称另运行过736项。

有界canary通过，5条新成功样本完整Graph GET再核验通过。19:49:36冻结观测中，常驻两个广告周期分别95.746秒与92.128秒，间隔48ms；清理仍执行时第二及第三广告周期已开始，证明清理没有成为广告周期屏障。worker无重启、无pause、新未知或重复task写意图；相对基线603条新成功，另1条原unknown只读恢复，148条旧Creative确认删除。API PID不变，旧清理timer仍disabled。

本次已证实执行调度与来源准备改善，但未重新测量足够长时间的全量“入队→提交绑定”分布，不能宣称历史2.32小时平均队列历时已降到某一值。保留4条既有unknown和所有失败回执；它们不是本次优化可自动重试的授权。