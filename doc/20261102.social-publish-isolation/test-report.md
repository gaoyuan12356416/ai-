# 测试结果
首轮跨模块 464 项通过，1 项环境相关跳过。新增跨日未开始排期保留规则后，定向回归和最终全套结果见本次发布回执。

生产验收：脚本在切换前冻结成功与未知日志的 ID、queue_id、status、attempt_count、post_id、URL、unknown 标记及发布时间的 SHA256；启动后、恢复计时器前要求一致。不会清空历史任务、回填 published 或重建旧计划。

最终调度定向回归 240 项通过；转发、Auto bridge 与 X sidecar 53 项通过。额外扩展到未修改的个人素材查询 gateway 时有 4 个原有失败；在修改前 35a910f 完整源码独立重跑同一组 9 项，复现相同 1 failure + 3 errors，属于旧测试夹具与线上 sources.py 读回格式不匹配。本次不修改/部署 sources.py 或其 gateway。

最终修改全部纳入后：517 项测试通过，1 项环境相关跳过（Windows 下依赖 Linux 的部署检查）。生产 drain 需考虑 X 常驻的 YouTube share worker，空闲线程数为 2；另查其队列无 queued/publishing。
