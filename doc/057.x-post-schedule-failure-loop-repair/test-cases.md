# 测试用例

## 测试范围

素材 FIFO、MySQL 瞬时异常、计划 known/unknown outcome、schedule lease、短剧精确恢复、数据库迁移和 X 既有发布回归。

## 测试数据

- 线上 Run 345 的 19 个精确 pool/material 及 rank 8 的 ja pool 820。
- 模拟首次/连续 CandidateQueryError。
- 23:44 claim、00:00 跨日和 2 小时租约边界。
- Run 274 的 14 条 queue/pool/content/episode manifest（离线临时 SQLite）。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | Run345 FIFO 精确回放 | 17 en + 2 ja | 不带/带 pool820 容量证据建计划 | 前者冲突，后者精确 19 队列 | P0 | 通过 |
| TC-002 | 容量证据防伪 | ja 未满、未来才满、素材 ID/来源语言/水合时间错误，或历史 available 错误 | 建计划 | 无因果证明则 409；历史码保留且不形成失败循环 | P0 | 通过 |
| TC-003 | 查询瞬时异常 | 第一次 query 失败 | 执行素材批次 | 重连一次后成功 | P0 | 通过 |
| TC-004 | 查询持续异常 | 两次 query 失败 | 执行素材批次 | failed_preflight，零 create/publish | P0 | 通过 |
| TC-005 | known 计划冲突 | create 返回 known 409 | 执行一次 tick | 只 create 一次并终态 | P0 | 通过 |
| TC-006 | unknown 计划响应 | create 响应丢失 | ledger 有/无队列读回 | 只发布已冻结队列；读回失败停止 | P0 | 通过 |
| TC-007 | 跨午夜租约 | 23:44 活跃 claim | 00:00/2h 后 due poll | 活跃不停止；超时才 stale | P0 | 通过 |
| TC-008 | 短剧 validate-only | 14 条精确 manifest | 离线验证媒体证据与 ledger | 零 DB/X 写入 | P0 | 通过；生产复核待部署 |
| TC-009 | 短剧原子 rearm | 全部媒体通过 | 离线 apply | 原队列/绑定不变，audit=14 | P0 | 通过；生产应用待部署 |
| TC-010 | 全回归 | 代码完成 | 执行 X 全测试 | 0 failure | P0 | 通过，796/796 |

## 回归范围

X daily、manual、schedule、material relay、drama pool、OAuth sidecar、SQLite ledger；不运行任何真实发布 canary。
