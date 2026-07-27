# 014.x-post-configured-accounts SA 代码评审

## 结论

通过。最终独立复核无 P0/P1 遗留；动态账号数、历史批次冻结、daily bearer 范围、失败审计、请求体边界、页面状态和运行时预算均已覆盖。

## 评审范围

- `scripts/x_post_daily_runner.py`
- `features/x_accounts/oauth_service.py`
- `features/x_accounts/client.py`
- `features/x_posts/service.py`
- X 账号列表与发布日志静态页面
- daily systemd/env 示例
- 相关单元、契约和 ledger 回归

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 修复 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | runner 发布汇总 | 仍以固定 3 条判断全批成功，9/9 会误报 | 改为按冻结 queue 数动态汇总 | 已修复 |
| CR-002 | P1 | failure audit/store | 预检失败未携带动态 `expected_count`，可能覆盖不同范围批次 | runner、client、sidecar、store 全链路透传并校验 | 已修复 |
| CR-003 | P1 | 同日恢复 | 3→9 配置变化可能误解释历史计划 | 有 queue 的历史计划按历史数量和身份恢复，不补建 6 条；无 queue 的旧范围失败批次冲突退出 | 已修复 |
| CR-004 | P1 | daily plan scope | 只校验账号集合，未校验配置顺序 | daily plan 必须与配置 tuple 顺序完全一致 | 已修复 |
| CR-005 | P1 | 请求体边界 | 合法 50 候选可能超过旧 256KiB/1MiB；检查批次可能超过通用 16KiB | plan 独立 2MiB，pool check 独立 128KiB，其他路由仍 16KiB | 已修复 |
| CR-006 | P1 | sidecar client | backend 失败记录 allowlist 丢弃 `expected_count` | 增加安全字段透传和客户端断言 | 已修复 |
| CR-007 | P1 | systemd | 9 次修复加 9 次顺序发布超出 180 分钟预算 | oneshot 超时提高为 360 分钟，生产 repair 上限同步为 9 | 已修复 |
| CR-008 | P2 | UI/DTO | 页面无法区分活跃与是否进入自动发布配置 | DTO 增加严格布尔字段，页面增加独立状态列 | 已修复 |
| CR-009 | P2 | 配置上限 | 1/50/51 边界最初只有设计、无直接执行证据 | runner、sidecar、store 补齐接受/拒绝测试 | 已修复 |

## 编译 / 验证结果

- Python 编译通过。
- 两个变更页面的内联 JavaScript 解析通过。
- X 全套离线回归：197/197 通过。
- `git diff --check` 通过。
- 独立复核结论：无 P0/P1/P2 遗留。
