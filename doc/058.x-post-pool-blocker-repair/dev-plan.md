# 实施计划

1. 以线上18d9cfb创建独立codex/x-pool-blockers-20260828工作树；不带入其他任务改动。
2. 增加只读账号暂停判断，接入schedule首轮过滤与最终事务/实际queue范围fence，保留全部配置审计。
3. 为逐队列deferred短剧接入受控GPU媒体准备，准备后重验账号；修复错误反馈契约。
4. 加固历史恢复CLI对源resource/URL的冻结一致性，扩展现有测试。
5. 运行py_compile、聚焦及Linux全X回归、diff检查；GitHub推送后从exact commit发布。
6. 对348/350做live validate-only→备份副本apply→live apply，且全部证据一致后允许自然timer消费。

多代理责任：主代理负责账号隔离、源身份恢复保护、文档与生产；媒体worker只改媒体准备/发布函数及对应测试；QA只改三个排期测试文件。共享文件按函数边界分工，不覆盖彼此改动。
