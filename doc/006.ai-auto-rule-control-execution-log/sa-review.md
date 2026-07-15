# SA 评审意见

## 结论

通过。2026-07-15 对“按业务日合并”和 partial 误导问题追加评审，设计已纳入以下门禁。

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
| SA-09 | P0 | 日汇总计数 | 直接累加多批会重复计算同一目标 | 列表只称“执行尝试”；不把累计值称为去重目标 | 已纳入 |
| SA-10 | P1 | 聚合键 | 只按日期/名称会串规则或人工执行 | 使用业务日+稳定规则标识+动作/层级/模式；缺标识不合并 | 已纳入 |
| SA-11 | P1 | 状态 | 历史 partial 会覆盖后续完成，且待复核/待续跑混为一谈 | 以链末态归并并拆分状态文案 | 已纳入 |
| SA-12 | P1 | 分页 | action limit 先截断会产生半条日汇总 | daily 先取有界窗口，丢弃可能不完整的最老组，再应用 group limit | 已纳入 |
| SA-13 | P1 | 详情性能 | 一次拼接全天 results_json 会放大读流量 | 主卡只带批次清单，原详情接口逐批懒加载 | 已纳入 |
| SA-14 | P0 | 多事件状态 | 同日后一事件成功会掩盖前一事件受阻/未完成 | 先按event链取末态，再保守汇总；全部闭环才完成 | 已关闭 |
| SA-15 | P1 | 人工日志 | 人工action携带event_key可能被误合并 | scheduled只由runner source/actor判定，event_key不决定是否聚合 | 已关闭 |
| SA-16 | P1 | 截断提示 | 分组limit与1000批读取上限共用同一提示 | 分开返回source_truncated、has_more_groups和raw has_more | 已关闭 |

## 决策记录

- 保留全局 200 作为安全上限，不将任务永久切成固定 200 个“目标”；它是每批计划量。
- 选择每账户 20、跨账户 4 并发，优先降低单账户/应用限流风险。
- Meta 执行事实与日志持久化解耦；不再因只读业务库同步警告把 Meta 成功误报为失败。
- MySQL 为日志主存储，SQLite 为 outbox/fallback，避免数据库短故障阻断调控。
- 生产旧规则数量已核验为 0，本次不扩展已停用旧路径。
- 200 仍是执行安全批次，不再作为日志主卡边界；底层批次保留，页面采用只读日汇总。
- 对历史 `version=1/event_key空/runner_reason空` 不回写猜测状态；只在读模型中显示“历史未完成/状态推导”，保留原始证据。

## PM 修订确认

PM/SA 已根据评审补齐错误分类、续跑终态、日期边界、日志版本与部署安全约束；可以进入开发与测试。
