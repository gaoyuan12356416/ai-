# SA 评审意见

## 结论

通过，以下评审问题均已纳入设计并关闭。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-01 | P0 | 日志 API | PyMySQL DATETIME 不能直接 JSON 序列化 | 读取层统一格式化为字符串并加测试 | 已关闭 |
| SA-02 | P1 | runner | continuation_attempt 可能跨事件累加，且上限判断早于零目标验收 | 仅同事件 partial 继承次数；零目标先验收 | 已关闭 |
| SA-03 | P1 | Meta 执行 | code4 后其他账户仍继续请求 | 应用级共享熔断器，未发项目记 deferred | 已关闭 |
| SA-04 | P1 | 执行安全 | Graph 缺 account_id 时仍可能写 PAUSED | owner 缺失或不一致都 fail closed | 已关闭 |
| SA-05 | P1 | 日志迁移 | 重跑 migration 会覆盖 runner 最终状态 | 默认仅插缺失 action，`--force` 才覆盖 | 已关闭 |
| SA-06 | P2 | 日志列表 | SQLite 列表加载完整 results_json | 列表只查轻量列，详情 lazy load | 已关闭 |
| SA-07 | P2 | 状态枚举 | error/blocked/partial 在 runner、DB、UI 不一致 | action log 使用 blocked/partial/executed，UI兼容 error | 已关闭 |
| SA-08 | P2 | 共享生产文件 | 仓库 app.py 非线上复合版 | 使用窄补丁、备份、线上 `--check`/diff | 已关闭 |

## 决策记录

- 保留全局 200 作为安全上限，不将任务永久切成固定 200 个“目标”；它是每批计划量。
- 选择每账户 20、跨账户 4 并发，优先降低单账户/应用限流风险。
- Meta 执行事实与日志持久化解耦；不再因只读业务库同步警告把 Meta 成功误报为失败。
- MySQL 为日志主存储，SQLite 为 outbox/fallback，避免数据库短故障阻断调控。
- 生产旧规则数量已核验为 0，本次不扩展已停用旧路径。

## PM 修订确认

PM/SA 已根据评审补齐错误分类、续跑终态、日期边界、日志版本与部署安全约束；可以进入开发与测试。
