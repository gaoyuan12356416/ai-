# SA 评审意见

## 结论

有条件通过。必须以“首次 X 写入前完整解析、加法迁移、历史 141 不扩权、resolved 永久不可变”为上线门槛。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | 数据结构 | 直接扩 `delivery_mode` CHECK 需重建核心队列表与多张外键账本 | 使用 companion route table 和逻辑 overlay，DB trigger fail closed | 已修订 |
| SA-002 | P0 | 发布入口 | 现行代码先 reserve log，再准备 deferred media | pending media 与路线解析前移到 reserve log/credentials/X write 之前 | 已纳入 |
| SA-003 | P0 | 幂等 | 现行 drama 计划重放忽略 delivery/relay | 重放校验逻辑路线与已冻结单向后继，resolved 读回 DB 权威路线 | 已纳入 |
| SA-004 | P1 | 修复策略 | 用非会员 standard 策略会把长片截成短片 | 原片时长决定 repair policy，最终修复文件决定路线 | 已纳入 |
| SA-005 | P1 | waiting | 旧 runner 假设发布响应必含 log/preview 且跨日可能 needs_review | waiting 为非终态、无日志响应；自然周期继续重查 | 已纳入 |
| SA-006 | P1 | 历史 | 现有 exact 141 人工恢复是严格审计例外 | companion 只绑定新 v1 队列，不修改既有 trigger/历史行 | 已纳入 |

## 决策记录

- ADR-001：不重建 `x_post_queue`；使用加法 companion 表保存 pending/waiting/resolved 状态。
- ADR-002：物理 delivery enum 与逻辑 API delivery 分离；外部仍看到 `duration_pending`。
- ADR-003：媒体 evidence 首次冻结后，waiting 仅重查账号能力，不重复修复。
- ADR-004：relay 选择在 `BEGIN IMMEDIATE` 内重算 lifetime load，账号 ID 作为稳定 tie-break。
- ADR-005：功能开关默认关闭；兼容部署和迁移健康通过后再开启。

## PM 修订确认

需求口径未改变。companion 表是为满足加法迁移与回滚安全采用的内部实现细节；对外状态、路由规则和幂等语义与确认方案一致。
