# 测试用例

## 测试范围

名称后缀、三层复制拓扑、失败隔离、created_data 名称一致性、API UTC+8 序列化、UI 固定时区、UTC+8 日期筛选、runner 日志与既有 V3/V2/playable 回归。

## 测试数据

- 固定时钟：`2026-07-17T06:55:00Z`（UTC+8 为 07-17 14:55）。
- 日界线：`2026-07-16T16:00:00Z`、`2026-07-17T15:59:59Z`、`2026-07-17T16:00:00Z`。
- 短名、空名、超过最大长度的来源名称。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 后缀格式 | 固定 UTC 时钟 | 生成复制后缀 | 等于 `[*copybyAI*07171455]` | P0 | 通过 |
| TC-002 | Campaign 复制命名 | 1 Campaign/2 Ad Set/N Ad | 执行 Stub copy tree | 全部新对象同后缀，来源对象不变 | P0 | 通过 |
| TC-003 | Ad Set 同 Campaign | carrier=`same_campaign` | 复制 Ad Set | 来源 Campaign 不改名，新 Ad Set/Ad 加后缀 | P0 | 通过 |
| TC-004 | Ad 独立承载 | isolated campaign/adset 两分支 | 复制 Ad | 所有新承载对象加后缀，复用父对象不变 | P0 | 通过 |
| TC-005 | 重命名回读 | Meta 更新成功 | 回读 name/status/关系 | name 精确一致且仍 PAUSED | P0 | 通过 |
| TC-006 | 重命名失败隔离 | 制造名称回读不一致 | 执行复制 | intent quarantined，不激活、不重复 copy POST | P0 | 通过 |
| TC-007 | 长名/空名 | 特殊来源名称 | 生成目标名 | 后缀完整、总长受控、空名有安全前缀 | P1 | 通过 |
| TC-008 | created_data 一致 | 完成多 Ad 复制 | 检查 ledger payload | campaign/adset/ad 名称均为 Meta 回读值 | P0 | 通过 |
| TC-009 | API UTC+8 | UTC 存储时间 | GET V3 API | 审计时间带 `+08:00`，响应头标记 UTC+8 | P0 | 通过 |
| TC-010 | UTC+8 日期筛选 | 选择 2026-07-17 | 检查 SQL 参数 | `[2026-07-16 16:00, 2026-07-17 16:00)` | P0 | 通过 |
| TC-011 | 内存仓储日期筛选 | 三个日界线对象 | 查询 7 月 17 日 | 仅 UTC+8 当日对象命中 | P1 | 通过 |
| TC-012 | UI 固定时区 | 浏览器使用非 UTC+8 | 格式化相同 API 值 | 始终显示 UTC+8；纯日期不跨日 | P0 | 通过 |
| TC-013 | runner 日志 | 执行 Stub tick | 读取 stdout JSON | 含 `ran_at` 的 `+08:00` 和 `display_timezone=UTC+8` | P1 | 通过 |
| TC-014 | 重放幂等 | intent 已完成 | 再次执行 | 0 Meta 写，不生成新后缀/对象 | P0 | 通过 |
| TC-015 | 共享回归 | 精确目标提交 | 跑 V3/V2/playable/部署测试 | 全部通过，无并行功能丢失 | P0 | 服务器待验证 |

## 回归范围

V3 live execution、repository、routes、UI、scheduler、deploy；V2 关键接口/日志；playable runtime guard；生产 service/timer/flags/HTTP。
