# 测试报告

## 测试结论

发布前本地与生产只读 canary 通过；尚待完成 GitHub-first 部署后的公网、
timer、LKG hash 和主服务无重启验收。

## 测试范围

- featured 排行、固定生产源、SQL 参数和元数据预算。
- 元数据筛选、公开字段、32 KiB 上限、原子 LKG。
- Nginx/系统服务契约、既有 resolver 回归。
- 390×844 真实 Chromium 动态卡片与 W2A 点击。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python featured + resolver | 43 | 43 | 0 | 0 |
| Node 页面逻辑断言 | 53 | 53 | 0 | 0 |
| Playwright 关键流程 | 1 | 1 | 0 | 0 |
| 生产只读 Top20 SQL canary | 2 | 2 | 0 | 0 |
| 部署后生产验收 | 待补 | 0 | 0 | 待部署 |

## 缺陷情况

- BUG-001：真实 MySQL 对早期 alias 排序报 1247；发布前发现并修复，
  未进入生产。
- 当前无未解决 P0/P1。

## 验证证据

- Python：43/43，包含 host/port/database fail-close、只读检查、非法 ID
  正则参数、metadata 预算、原子失败保留旧文件及搜索回归。
- Node：53/53，包含固定 W2A 参数、透传、精确 5 条、非法/未来时间和
  72 小时陈旧。
- Playwright：
  `2026-07-26` 动态缓存渲染 5 个链接；点击 `BQ3Y3JcLWA` 前先请求同源
  resolver，再到标题为 `Wrong Sister in His Bed` 的 W2A 页；URL 保留
  `af_adset_id=XXX`，初始 href 为同页安全锚点，本页无 JS/CSP error。
- 生产只读 SQL：EXPLAIN `key=as/type=ref`；含 LIMIT 前正则的 Top20
  连续两次 1.487s / 1.408s，签名均 `2143235fdcee2b8a`。

## 遗留风险

- insight 在次日中午后仍回填；通过 15:30 主刷与 18:00 对账降低偏差。
- 封面 CDN 单图失败时展示本地占位；不影响卡片点击。
- FakeCursor 不验证真实 MySQL 语法，故生产只读 SQL canary 是长期发布闸门。
- 本期不新增外部通知凭据；刷新失败会进入 systemd failed/journal，前端在
  72 小时后自动回退。若需主动 Feishu 告警，应另行确认通知群和凭据边界。

## 发布建议

满足先生成快照、再发布 Nginx/前端、最后启用 timer 的顺序后可发布。
部署后必须补齐公网响应头、快照 hash、故障注入、timer 和主 API
`NRestarts` 证据。
