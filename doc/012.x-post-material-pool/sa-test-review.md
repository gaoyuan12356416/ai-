# 012.x-post-material-pool SA 测试用例评审

## 结论

通过。“池先存在再走非池 queue”“精确 Dramawave 产品门禁”“summary.available 口径”、原始池 scan limit 和 205 条检查分批均已补充实现/断言；最终工作树完整离线回归 139/139 通过。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | TC-004/005 | 原测试只覆盖 queue 先存在，未覆盖反向竞态 | 增加 pool-first 后 legacy/canary enqueue 的 service+trigger 双层负例 | 已关闭并通过 |
| STR-002 | TC-008 | manual selector 测试仅断言 ID/type/delete/duration | fake 行加入 product，并覆盖其他产品拒绝 | 已关闭并通过 |
| STR-003 | TC-015 | item availability 与 summary 可能使用不同 SQL | 同一 fixture 同时断言 item、filter total、summary.available | 已关闭并通过 |
| STR-004 | TC-016/018 | “不足三条整批不发”必须同时覆盖 selector 与媒体阶段 | 断言 run/queue/short link/Create Post 调用数 | 已覆盖主体 |
| STR-005 | TC-021/022/023 | 不能只断言 queue status | 同时断言 pool 主状态、派生状态、published_at 和再次 available 结果 | 已覆盖主体 |
| STR-006 | TC-026/029 | 页面功能不能替代安全契约 | 覆盖 Cookie admin、同源、no-store、DOM 文本和 URL allowlist | 已覆盖主体 |
| STR-007 | TC-030 | 空库迁移不足以代表生产 | 用 legacy canary/queue 副本验证触发器、索引和幂等 | 离线 fixture 已覆盖，生产副本待部署 |
| STR-008 | TC-031 | 候选上限 50 曾被误用为原始池读取上限 | runner 改为按 scan limit 读取最老 1000，再保留最多 50 个合规候选 | 已关闭并通过 |
| STR-009 | TC-033 | 拒绝结果可超过 Sidecar 单次 100 条限制 | 用 205 条断言 runner 按 100/100/5 分批 | 已关闭并通过 |

## QA 修订确认

`test-cases.md` 已补齐上述用例，`test-report.md` 记录最终代码修订后的 139 项完整回归和未执行的生产项。
