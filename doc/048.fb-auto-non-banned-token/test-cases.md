# 测试用例

## 测试范围

Page 组统计、Page 快照、名称、执行凭证、历史冻结边界、生产服务健康与回滚。

## 测试数据

- 状态：`-1、0、1、2`。
- Token：非空、空字符串、重复 Token。
- 生产 Page 组 62：13 个 Page；旧口径 8，新口径预期 12，被封 1。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 组统计谓词 | 生成 list_groups SQL | 检查 SQL | 使用 `p.status<>1`，无 `p.status=0` | P0 | 已通过 |
| TC-002 | Page 快照谓词 | 生成 list_pages SQL | 检查名称与计数子查询 | `pn/p.status<>1` 且 Token 非空 | P0 | 已通过 |
| TC-003 | 执行凭证谓词 | 调用 eligible_credentials | 检查 SQL | 使用 `status<>1`，状态 1 被排除 | P0 | 已通过 |
| TC-004 | Token 安全条件 | 重复与空 Token 数据 | 调用凭证查询 | 空 Token 不入选，重复 Token 去重 | P0 | 已通过 |
| TC-005 | status=2 规则 | 状态 2 且 Token 非空 | 运行资格 SQL | 纳入候选 | P0 | 已通过 |
| TC-006 | FB 专项回归 | 本地完整源码 | 执行 discover | 全部通过 | P0 | 已通过：129/129 |
| TC-007 | 生产只读 Page 池 | 只读 63350 | 对比旧/新谓词 | 13/8/12/被封1 | P0 | 已通过 |
| TC-008 | 历史冻结不变 | 现有 run 17-21 | 部署前后读取 | 保持 8 可发/5 跳过 | P1 | 已通过 |
| TC-009 | 服务发布 | 精确 GitHub SHA | 切换 release 并重启窄服务 | health 正常、timer 恢复、无运行中断 | P0 | 已通过 |
| TC-010 | 无真实发帖验收 | 不调用 run-now | 核对 attempt/ledger 与自然时钟 | 不因测试额外创建 Graph 请求 | P0 | 已通过 |
| TC-011 | 动态状态重读 | 同一执行器连续执行两次，凭证返回不同 | 运行 execute 两次 | 每次均重新调用 `eligible_credentials()` | P0 | 已通过 |
| TC-012 | 明确失败换 Token | 第一个 Token 明确失败、第二个成功 | 执行任务 | 使用两个不重复 Token，最终 submitted | P0 | 已通过 |
| TC-013 | 未知结果不换 Token | 第一个 Token 返回 unknown | 执行任务 | 仅调用一次 Graph，任务进入 unknown | P0 | 已通过 |

## 回归范围

- 所有 `test_fb_auto*.py`。
- X/TT 合并基线（沿用现有部署测试集合）。
- 生产 SQLite `quick_check`、任务状态、timer、health、日志。
