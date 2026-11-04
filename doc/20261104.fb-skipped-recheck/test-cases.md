# 测试用例

## 恢复与防重复

1. 两批目标旧 unknown_block 且未选材任务，权限/来源有效且无任何执行证据时，原 task/run ID 保持不变并进入 planned。
2. 同一恢复 operation 重复调用保持幂等，不新建任务、不延期、不重新选择已排队素材。
3. 每日 0 次 Page、已有 published/unknown/submitted、任意 attempt/ledger/claim/operation/hold 保持不变。
4. 两批同 Page 的恢复须顺序在事务内预留，不能选到同一素材；已发布和未知素材不入选。
5. 新版参数/权限变化、无可用素材单项保留原因，不阻止其他正常 Page 恢复。
6. 恢复窗口过期按现有终止规则处理；普通自动任务原迟到宽限不变。

## 中央调度

7. 原 completed/metadata_only、generation=0、source_status=skipped 的 prepare 观测，仅凭有效 native 恢复标记且全源无历史执行证据，恢复原 job ID。
8. 重复 offer 只产生一条恢复事件；已领取后不重排。
9. 普通 status 重置、failed/published/unknown、任何 generation/intent/claim/lease/receipt/hold 不得触发恢复。
10. 中央 operation、素材隔离与 native ledger/operation/claim/hold/Graph ID 等证据保留；拒绝恢复不得清除隔离资源。
11. 恢复后的任务完成制作，依原发布链路推进；未知核验不能卡住其他 Page/素材。

## 未来调度

12. 存在旧 planned/running/unknown 时，其他正常 Page 可创建后继批次；原 slot 唯一键与素材占用仍防重复。
13. unknown 素材继续永久排除，同剧其他素材只受既定剧冷却时间窗限制。
14. 核验 GET 超时/异常仍返回待核验；后台队列有界，采集和其他任务不等待，绝不执行重复 POST。

## 已执行离线范围

`python -m unittest scripts.test_pipeline_fb_task_isolation -q`：20 项通过，覆盖 7–10、11/14 的隔离基础及既有后台核验回归。其余用例由主工作流 native 测试和生产预演核验。
