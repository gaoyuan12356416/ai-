# 测试用例

## 测试范围

随机性/稳定性、语言、时长、relay 资格、FIFO/原子性、状态机、迁移、manual/X Auto/drama 回归。

## 测试数据

仅临时 SQLite、mock account/token entitlement、mock media probe/downloader；无真实 X API 写入。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 稳定 seed | 同一 slot/账号/语言 | 执行两次 shuffle | 顺序完全一致 | P0 | 通过 |
| TC-002 | 可注入随机 | 注入 reverse shuffler | 执行配对 | 精确得到指定 permutation | P0 | 通过 |
| TC-003 | 140.0 秒 | 非会员 target | preflight | direct | P0 | 通过 |
| TC-004 | 140.001 秒 | 非会员 target + 两个同语言 relay | preflight | target 不变，随机 relay source | P0 | 通过 |
| TC-005 | 无 relay | FIFO long 后有 short | preflight | 立即 failed_preflight，短素材不补位，零写入 | P0 | 通过 |
| TC-006 | 同语言 target | en/ja 混合账号和素材 | 配对 | 不跨语言 | P0 | 通过 |
| TC-007 | 跨语言 relay | en target 仅 ja relay | create plan | failed，零 queue | P0 | 通过 |
| TC-008 | 整批原子性 | material long relay 不可用 | create plan | run/queue/binding/ledger 无部分提交 | P0 | 通过 |
| TC-009 | 重启冻结 | 同 slot 重放 create | 查询/重放 | queue/material/target/relay ID 不变 | P0 | 通过 |
| TC-010 | pool 状态 | relay source 已发、target 未 Repost | 查 pool | unpublished | P0 | 通过 |
| TC-011 | Repost 成功 | target Repost 确认 | mark_reposted | pool 与 queue 同事务 published | P0 | 通过 |
| TC-012 | manual 污染 | 非 schedule material relay enqueue | 提交 | 应用层拒绝 | P0 | 通过 |
| TC-013 | short relay | duration=140 relay | create plan | 应用/trigger 拒绝 | P0 | 通过 |
| TC-014 | OAuth 精确 relay | frozen relay 是可选列表第二项 | create request | 保留第二项，不替换为第一项 | P0 | 通过 |
| TC-015 | OAuth relay 漂移 | frozen relay 不在当前列表 | create request | 整批失败，不静默改路由 | P0 | 通过 |
| TC-016 | drama least-load | 既有多 relay drama | 回归 | 平衡顺序不变 | P0 | 通过 |
| TC-017 | unknown fence | source/repost ambiguous | 回归 | needs_review，禁止重发 source | P0 | 通过 |
| TC-018 | migration | 有 relay 历史 SQLite | ensure 两次 | 幂等、历史 queue/ledger 不变 | P1 | 通过 |
| TC-019 | 单 Premium target FIFO | 最新 long、较旧 short | preflight | 选择最新 long direct | P0 | 通过 |
| TC-020 | DB relay triggers | 直接 SQL insert/update 多个非法组合 | 写入 | 无 parent、短 relay、同账号、direct 带 relay 全部拒绝；合法 schedule relay 通过 | P0 | 通过 |
| TC-021 | zero-attempt reassign | material queue 未开始 source attempt | 重选两次 | 仅同语言且结果稳定 | P1 | 通过 |
| TC-022 | attempt fence | source attempt 已开始 | 重选 relay | `x_post_relay_reassignment_fenced` | P0 | 通过 |

## 回归范围

`test_x_post_schedule_runner`、`test_x_post_premium_relay_repost`、`test_x_post_multi_schedule_store`、`test_x_account_language_routing`、`test_x_accounts` 与全部 `test_x_*.py`。
