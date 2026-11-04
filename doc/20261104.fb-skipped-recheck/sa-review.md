# SA 设计评审

2026-11-04，基线 44843eb。

## 结论

按原任务恢复，先预演再执行；未知发布只核验，不重新 POST。批准在指定未执行 skipped 任务范围恢复排队。Page 867618086440072 每日 0 次不变。

## 关键问题与决策

| 问题 | 处理 |
| --- | --- |
| 已有中央 completed/metadata_only 观测不会随 native 状态复位自动恢复 | 增加仅识别 native `selection_json.recovery` 的窄恢复路径；原 generation=0、source_status=skipped、无执行或回执历史才恢复原 prepare job |
| 已知 Page+material 的 unknown 可能仍通过剧目冷却永久挡住同剧其他素材 | 同素材 unknown 继续永久隔离；剧目按原 24 小时窗口，以 started→planned→created 时间判断 |
| 单个旧批次未完成会触发全模板 previous_run_backlog 拒绝 | 移除全模板拒绝，保留容量、slot 唯一键、事务内素材占用及 Page 实际写入互斥 |
| 原任务超过默认迟到宽限会在恢复后立即失效 | 使用明确有期限的恢复窗口；重复执行不得延期；普通自动任务原迟到规则不变 |
| native 跳过记录可能已被别的进程推进 | BEGIN IMMEDIATE 内重新读取并条件更新；任何 attempt、ledger、Graph ID、unknown、claim、operation、hold 都不恢复 |

当前后台核验已有 2 个独立线程、32 项有界队列及每次 GET 10 秒超时；核验异常只记录错误，后续正常素材发布无需等待核验。禁止为了恢复而清除未知素材隔离。

## 并行实现范围

本评审工作流只修改 pipeline `adapters.py/core.py` 的原 skipped 观测恢复及对应离线测试；native 恢复、调度政策、部署和生产核验由主工作流完成。
