# SA 代码评审

## 结论

当前实现可进入发布前验证。无未解决 P0/P1；生产 SQL 语法必须保留真实
63350 canary，不能只依赖 FakeCursor 单测。

## 评审范围

- 离线只读排行、元数据筛选、快照原子替换与数据盘门禁。
- systemd 用户/环境隔离、Nginx 精确静态路由。
- 前端动态卡片、陈旧策略、参数透传和点击前 resolver 校验。
- 单测对 SQL、LKG、公开字段及既有搜索回归的覆盖。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | 排行 SQL | 早期版本对 SELECT alias 做 `BINARY` 排序，生产 MySQL 报 1247 | 改为 `MIN(i.data_source_id)` 与 `MIN(BINARY i.data_source_id)`；加生产 canary | 已修复 |
| CR-002 | P1 | 排行 SQL | 非法 ID 在 LIMIT 后过滤，可能挤掉合法 Top20 | SQL 中于 LIMIT 前加已实测的二进制正则 | 已修复 |
| CR-003 | P2 | 元数据查询 | buffered 结果无应用层预算 | 候选最多 20，SQL LIMIT 及 500 行/候选总预算 | 已修复 |
| CR-004 | P2 | 数据源 | 仅强制 63350，仍可能误连错误主机/库 | 固定 host/port/database，实例及事务双只读检查 | 已修复 |
| CR-005 | P1 | systemd | 主服务整份环境和 root 身份扩大秘密与文件权限 | 独立无登录用户、专用 0600 DB 环境、严格 sandbox | 已修复 |
| CR-006 | P1 | 前端缓存 | LKG 可无限陈旧，旧剧可能下架 | 最多陈旧 72 小时，普通点击前 resolver fail-close | 已修复 |
| CR-007 | P2 | 前端卡片 | 外部 href 会让中键/长按绕过普通 click handler | 初始 href 改为同页锚点，W2A 目标只在 resolver 成功后使用 | 已修复 |
| CR-008 | P2 | 前端时间 | 只设最大 age 会接受未来生成时间或异常 source_date | 限制未来 24 小时及 source_date 相对上海昨日的窗口 | 已修复 |

## 编译 / 验证结果

- `python -m py_compile`：通过。
- TT featured + resolver Python 回归：43/43 通过。
- Node 页面逻辑：53 项断言通过。
- 390×844 Playwright：5 个动态链接、真实封面、参数透传和点击后 W2A
  内容匹配通过。
- 生产排行 SQL（含正则）：两次只读执行 1.487s / 1.408s，Top20 签名一致，
  EXPLAIN 使用 `as` 索引。最终发布后仍需以 release 脚本再做完整 canary。
