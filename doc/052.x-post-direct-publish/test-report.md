# 测试报告

## 测试结论

离线测试通过。实现满足“定时素材/短剧建队前 0 次媒体 download/probe/repair，实际发布时一次校验并立即上报”的合同；独立 QA 未发现 P0/P1。生产同批次续跑结果在部署后追加。

## 测试范围

素材/短剧轻量规划、deferred queue/迁移、Premium Relay、实际 W2A/category、历史 preflight 兼容、known/unknown/429、run 聚合、claimed 恢复和 X 全模块回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 独立 focused QA | 259 | 259 | 0 | 0 |
| X 全量自动化 | 731 | 729 | 0 | 0（条件 skip 2） |
| 编译/静态检查 | 2 | 2 | 0 | 0 |

## 缺陷情况

- BUG-001：建队前重复媒体预检导致到点不发布，已修复；生产验收待部署。
- 代码评审发现 CR-001/CR-002 两项阻断边界，均已修复并回归。

## 验证证据

- 主代理本地全量：`Ran 729 tests in 39.919s`，`OK (skipped=2)`。
- 独立 QA：focused 259/259；全量 729/729、skip 2；`py_compile`/diff check 通过。
- deferred 实际发布 mock：routing hint 141、真实 duration 45.25，最终 `af_channel=short`、category `tweet_video`。
- 绑定坏剧 + 健康兄弟：只为健康账号子集建队并发布，坏剧绑定/进度/错误状态不变。

## 遗留风险

- deferred 不再执行建队前 GPU repair；原始媒体不符合 X 合同时会成为明确失败并继续下一队列。
- 短剧未知时长对非 Premium 目标保守 Relay，短视频也会多一次源 Post/Repost。
- 生产不创建额外 canary；真实 run 271/274 是唯一自然验收范围。

## 发布建议

建议发布。必须先复核 run 271/274 仍为 claimed、queue/log/attempt/unknown 全 0，完成 SQLite/Token/unit/release 备份后再续跑同一批次。
