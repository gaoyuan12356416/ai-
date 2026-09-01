# 测试报告

## 测试结论

代码、专项、全量 X 回归、独立审查、feature-off 生产迁移与双端启用均通过；自然排期业务验收仍在观察。

## 测试范围

schema/store/resolver、媒体修复、Sidecar call-order、scheduler fixed/random/waiting、DTO/UI、历史恢复和全量 X 回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 变更前基线聚焦 | 259 | 259 | 0 | 0 |
| UI 聚焦 | 48 | 48 | 0 | 0 |
| Store 路由/崩溃/公平性专项 | 128 | 128 | 0 | 0 |
| 最终全量 X | 892 | 890 | 0 | 2（环境跳过） |
| 最终全仓 discovery | 1455 | 1448 | 5 | 2（环境跳过） |

## 缺陷情况

独立审查发现并关闭 6 类 P1：waiting 队列被 limit 饿死、冻结剧集污染下一 slot、waiting 媒体证据可直接 SQL 改写、resolved 后跨日崩溃误停、resolved relay ledger 可删除/搬移，以及 rollout 遗漏同库 writer。另关闭 browser DTO 暴露 SHA/size、未使用内部 route 元数据、旧/非短剧日志误标新路线及 feature-off timer 顺序问题。当前无已知未关闭代码 P0/P1/P2。

## 验证证据

- 基线 commit：`955e54b64f137ec4298cba39ccf9e443dc4a4e73`。
- 已精确合并生产并行 commit `401069b2e35e56192c33efac623bf24ddee57a56`，保留任务来源筛选/展示，并与时长路线字段共同回归。
- 测试方式：本地 Python unittest、临时 SQLite、mock HTTP/X；禁止真实平台写入。
- 生产基线：quick_check=ok、FK=0、无 queued/publishing；历史 unknown 隔离保留。
- 生产 release：`e64742213f171a49d5befd88c3507ef25f42c63b`；Sidecar 与 scheduler 运行时开关均为 true，原 active 的 6 个 timers 已恢复。
- 生产迁移：1142 queue、1142 publish log、409 repost ledger、76 drama pool 计数不变；新增 route 表 0 行、6 个约束触发器；feature-off/on 两阶段均 quick_check=ok、FK=0、inflight=0、历史 unknown=1。
- 生产备份：`/mnt/data-disk/x-post-automation/backups/duration-routing-20260901T113528Z-pre-401069b2e35e56192c33efac623bf24ddee57a56-to-e64742213f171a49d5befd88c3507ef25f42c63b`，含在线/停写 SQLite、unit/env 与 Main composite 文件；目录权限为 0700，配置内容未输出。
- 全仓 5 个错误均来自 TT 基线：`_TTDramawaveCandidateSelector._violation_counts` 缺失及 `_pool_material_rows(... allow_long_duration=...)` 合同不匹配；同样 5 个错误已在干净 `955e54b` 复现，本次未修改 TT 代码，判定无新增全仓回归。

## 遗留风险

自然首条短 direct 与长 relay 的平台结果只能在上线后由自然排期产生，不能由 mock 代替。

## 发布建议

GitHub-first 部署、feature-off 迁移与 feature-on 恢复已完成。最终业务通过仍取决于首个自然短 direct 与长 relay 的 queue/ledger/X 对账；只读心跳已安排，禁止测试帖和人工重试。
