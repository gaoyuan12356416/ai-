# SA 代码评审

## 结论

通过，未发现未关闭 P0/P1。最终以全量测试结果为准。

## 评审范围

- runner 稳定 seed、语言 bucket、FIFO 扫描与 relay preflight
- OAuth relay 当前资格与 frozen ID 复核
- Store payload/trigger/FIFO replay/atomic plan/reassign/repost 状态
- 测试与旧合同更新

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | store relay trigger | material relay 放宽必须限制 parent | 要求 `schedule_run_id IS NOT NULL` | 已解决 |
| CR-002 | P0 | store relay options | 多语言 flatten 可能跨语言 | 对每个 queue 用 frozen language 过滤 | 已解决 |
| CR-003 | P0 | mark_reposted | 原实现只推进 drama pool | 同事务推进 material pool | 已解决 |
| CR-004 | P1 | create replay | 旧比较未包含 material relay 映射 | 重放比较 delivery mode + relay ID | 已解决 |
| CR-005 | P1 | zero-attempt reassign | material 不应沿用 drama least-load | 以 queue identity SHA-256 稳定选取 | 已解决 |
| CR-006 | P0 | FIFO replay | 新版曾允许 `x_long_video_requires_premium` 被更旧短素材绕过 | 恢复 `remaining_premium_ids` 防线并对任意最新 x_long fail closed | 已解决 |
| CR-007 | P0 | relay language | material relay option 缺语言时可能无法证明不跨语言 | create/reassign 强制合法 canonical language，drama 保持兼容 | 已解决 |
| CR-008 | P0 | SQLite guard | 仅应用测试不足以证明 trigger | 直接 SQL insert/update 覆盖四类非法组合与合法组合 | 已解决 |

## 编译 / 验证结果

见 test-report.md。`git diff --check` 当前通过，仅有 Windows CRLF 提示，无 whitespace error。
